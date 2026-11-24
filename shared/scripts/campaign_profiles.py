"""Acquire bounded optional evidence after completed measurements are durable."""

from collections import Counter
from pathlib import Path
import time
from adaptive_policy import enabled, record_evidence_anchor
from acquisition_limits import scope as acquisition_scope, limited
from acquisition_cost import event as cost_event
from brief_facts import classify_iteration, source_features
from compiler_evidence import for_target
from decision_learning import active as decision_enabled
from capture_history import preserve
from evidence_router import fresh_calibration
from evidence_scope import primary_target, requires_roi, roi_scope
from experiment_support import load, write
from kernel_evidence import attach_advice
from kernel_identity import capture_selector, simple_base
from screening_quality import diagnostic_disagreement


def profile_candidates(plan, run, state, baseline, ids, phase, session, evidence, needs, provider,
                       *, compiler_evidence, select_evidence, route_evidence, run_iteration_profile):
    by_id, name, force_ncu = (session[k] for k in ("by_id", "name", "force_ncu"))
    profiles, reference_profiles = [], []
    baseline_kernel = (state.get("kernel_profiles") or {}).get("baseline")
    baseline_anchor = state.get("baseline_activity") or {}
    for candidate_id in ids:
        parent_id = state["candidates"][candidate_id]["parent"]
        parent_kernel = (baseline_kernel if parent_id == "baseline" else
                         ((state["candidates"].get(parent_id) or {}).get("profile") or {}).get("ncu"))
        from selection_objective import policy_row
        sample = policy_row(state, by_id[candidate_id])
        resources, acquisition = None, None
        acquisition_started = time.perf_counter()
        profile_started = acquisition_started
        try:
            with acquisition_scope(plan, run):
                if enabled(plan) and phase == "screen":
                    from skill_runtime import feedback
                    if decision_enabled(plan):
                        from decision_learning import feedback
                    feedback(plan, run, state, sample)
                    compiler_evidence(plan, run, baseline, "baseline")
                    resources = compiler_evidence(plan, run,
                        Path(state["candidates"][candidate_id]["workspace"]), candidate_id, parent_id)
                    previous_resources = load(run / "compiler" / (parent_id + ".json"), {})
                    before_names = {simple_base(x.get("name")) for x in previous_resources.get("all_kernels", [])}
                    if before_names:
                        resources["new_kernel_bases"] = sorted({base for x in resources.get("all_kernels", [])
                            if (base := simple_base(x.get("name"))) and base not in before_names})
                    parent_profile = (state["candidates"].get(parent_id) or {}).get("profile") or {}
                    if not primary_target(plan, parent_profile):
                        parent_profile = {}
                    anchor = ((parent_profile.get("ncu") or {}).get("kernel") or (parent_profile.get("next") or {}).get("name") or
                              (parent_profile.get("target") or {}).get("name") or (baseline_kernel or baseline_anchor).get("kernel"))
                    acquisition = select_evidence(plan, state, sample, for_target(resources, anchor), evidence)
                    automatic = evidence == "auto" and not needs and not provider and candidate_id not in force_ncu
                    if automatic and decision_enabled(plan):
                        from decision_policy import choose
                        acquisition = choose(plan, run, state, sample, acquisition,
                                             session.get("campaign_spent") if not session.get("campaign_cached") else None,
                                             route_evidence)
                    elif automatic:
                        from skill_runtime import replan
                        acquisition = replan(plan, state, sample, acquisition)
                    if provider:
                        acquisition["requested_provider"] = provider
                    if candidate_id in force_ncu:
                        acquisition = {"tool": "kernel", "reason": "agent-forced-replay",
                                       "requested_provider": "pw-nsight-compute", "screening_only": True}
                    if not (automatic and decision_enabled(plan)):
                        acquisition = route_evidence(plan, state, acquisition, needs)
                    from objective_acquisition import request
                    acquisition = request(state, candidate_id, acquisition, automatic, run=run)
                    if acquisition.get("tool") in {"kernel", "pc-sampling"} or acquisition.get("provider") == "pw-gpa" or candidate_id in force_ncu:
                        ancestor = parent_id
                        while ancestor != "baseline":
                            meta = state["candidates"].get(ancestor) or {}
                            profile = meta.get("profile") or {}
                            target = ((profile.get("ncu") or {}).get("kernel") or (profile.get("next") or {}).get("name") or
                                      (profile.get("target") or {}).get("name"))
                            if (target and profile.get("status") == "complete" and
                                    (acquisition.get("question_origin") == "agent" or primary_target(plan, profile))):
                                acquisition.update({**capture_selector(target, resources, acquisition.get("tool")),
                                                    "target_name": target,
                                                    "target_anchor": ancestor,
                                                    "target_scope": ((profile.get("acquisition") or {}).get("target_scope") or profile.get("scope", "profile-defined"))})
                                break
                            ancestor = meta.get("parent", "baseline")
                        if not acquisition.get("target_regex") and anchor:
                            acquisition.update({**capture_selector(anchor, resources, acquisition.get("tool")),
                                                "target_name": anchor,
                                                "target_anchor": "baseline",
                                                "target_scope": load(run / "profile.baseline.json", {}).get("scope", "baseline-anchor")})
                if acquisition is not None:
                    cost_event(run, "routing-and-static", time.perf_counter() - acquisition_started,
                               candidate=candidate_id, provider=acquisition.get("provider"))
                profile_started = time.perf_counter()
                incumbent = (acquisition or {}).get("policy_target") or (state.get("best") or {}).get("id")
                if (acquisition and acquisition.get("reason") == "plateau-needs-mechanism-evidence" and
                        acquisition.get("tool") != "none" and incumbent and incumbent != candidate_id):
                    from evidence_acquisition import acquire
                    reference = acquire(plan, run, state, baseline, incumbent, acquisition=acquisition, record_cost=False)
                    reference_profiles.append(reference)
                    from iteration_packet import reference_observations
                    capsule = {"id": candidate_id, "status": "deferred", "reason": "incumbent-evidence-acquired",
                               "reference_evidence": {"id": incumbent, "status": reference["status"],
                                   "provider": (reference.get("acquisition") or {}).get("provider"),
                                   "detail": str(run / "candidates" / incumbent / "evidence.json")}}
                    if decision_enabled(plan):
                        capsule["reference_evidence"]["observations"] = reference_observations(reference)
                elif (acquisition or {}).get("reason") == "primary-kernel-timing-already-observed":
                    capsule = state["candidates"][candidate_id]["profile"] | {"cache": "hit"}
                else:
                    capsule = run_iteration_profile(
                    plan, run, candidate_id, by_id[candidate_id], parent_id,
                    baseline_kernel, parent_kernel, force_ncu=candidate_id in force_ncu,
                    phase=phase, acquisition=acquisition)
        except Exception as exc:
            if not limited(plan):
                cost_event(run, "profile", time.perf_counter() - profile_started, candidate=candidate_id,
                           status="failed", provider=(acquisition or {}).get("provider"))
                raise
            failure = run / "raw/experiment" / (name + "-" + candidate_id + "-profiling-failure.json")
            capsule = {"id": candidate_id, "status": "blocked", "reason": "profiling-question-unresolved",
                       "artifact": str(failure), "unanswered": [str(exc)[:350]],
                       "new_benchmark_runs": 0}
            write(failure, capsule | {"exception": type(exc).__name__, "error": str(exc),
                                     "acquisition": acquisition})
        capsule["spent_s"] = time.perf_counter() - profile_started
        if capsule.get("fallback_attempts"):
            acquisition = capsule["acquisition"]
        cost_event(run, "profile", capsule["spent_s"], candidate=candidate_id,
                   status=capsule.get("status"), provider=(acquisition or {}).get("provider"))
        if acquisition:
            capsule["acquisition"] = acquisition
            capsule["static"] = resources
            capsule["acquisition_wall_s"] = time.perf_counter() - acquisition_started
            from skill_runtime import remember
            remember(plan, run, state, capsule)
            provider = acquisition.get("provider")
            if provider and fresh_calibration(capsule):
                elapsed = capsule["acquisition_wall_s"] if decision_enabled(plan) else capsule["spent_s"]
                state.setdefault("acquisition_costs", {}).setdefault(provider, []).append(elapsed)
                if acquisition.get("cost_key"):
                    state["acquisition_costs"].setdefault(acquisition["cost_key"], []).append(elapsed)
            if not requires_roi(plan) or roi_scope(capsule.get("scope")):
                record_evidence_anchor(state, acquisition, capsule, by_id[candidate_id])
            if capsule.get("diagnostic_metric") is not None:
                by_id[candidate_id]["ranking"] = diagnostic_disagreement(
                    by_id[candidate_id].get("ranking") or {}, by_id[candidate_id].get("metric") or {},
                    capsule["diagnostic_metric"], plan["metric"].get("direction", "lower"), plan["sampling"])
        history = Counter()
        for meta in state["candidates"].values():
            if meta.get("result"):
                for mechanism in classify_iteration(meta):
                    history[mechanism] += 1
        observed = max(((meta.get("result") or {}).get("improvement") or {}).get("point", 0)
                       for meta in state["candidates"].values())
        if capsule.get("ncu") and capsule["ncu"].get("status") in {None, "complete"}:
            ncu = capsule["ncu"]
            target_doc = load(run / "candidates" / candidate_id / "profile" / "source-targets.json", {})
            features, _ = source_features(run, (target_doc.get("targets") or [])[:7])
            profile_doc = load(run / "candidates" / candidate_id / "profile" / "profile.json", {})
            scope = profile_doc.get("scope") or {}
            pre_copy = ((scope.get("copies") or {}).get("before_roi") or {})
            if (pre_copy.get("union_s") or 0) > 0:
                metric_name = str((plan.get("metric") or {}).get("name") or "").lower()
                features.append("setup-transfer-heavy" if any(
                    token in metric_name for token in ("whole", "wall", "end-to-end", "total"))
                    else "setup-transfer-collateral")
            capsule["ncu"] = attach_advice(
                ncu, ncu.get("hotspot_fraction"), dict(history), observed, features)
        if decision_enabled(plan):
            learning = state.get("skill_runtime") or {}
            if learning.get("last_capture") == candidate_id:
                capsule["capture_binding"] = learning.get("binding")
            preserve(run, candidate_id, capsule, (session.get("result") or {}).get("fingerprint_after"))
        prior_profile = state["candidates"][candidate_id].get("profile") or {}
        if not (enabled(plan) and phase == "confirm" and capsule.get("status") == "deferred"
                and prior_profile.get("status") == "complete"):
            state["candidates"][candidate_id]["profile"] = capsule
        profiles.append(capsule)
    return profiles, reference_profiles
