"""Acquire missing evidence for a preserved measured snapshot, without benchmarking it again."""

from pathlib import Path
import time

from adaptive_policy import enabled
from acquisition_cost import event
from brief_current import current_evidence
from campaign_io import campaign_fingerprint, REPO
from compiler_evidence import collect
from evidence_router import route, fresh_calibration
from experiment_support import load
from experiment_state import save_state
from experiment_report import refresh_brief
from iteration_profile import run_iteration_profile, _locate_source
from kernel_identity import capture_selector
from acquisition_limits import bounded, limited


def validate_snapshot(run, state, candidate):
    entry = next((b for b in reversed(state.get("batches", [])) if candidate in b.get("candidates", [])), None)
    if not entry:
        # Older interrupted evaluations saved timing before adding the batch index.
        stored = ((state.get("candidates") or {}).get(candidate) or {}).get("result") or {}
        for path in sorted((run / "batches").glob("screen-*/campaign-result.json"), reverse=True):
            result = load(path, {})
            row = next((r for r in result.get("candidates", []) if r.get("id") == candidate), None)
            compact_metric = {k: (row.get("metric") or {}).get(k) for k in (stored.get("metric") or {})} if row else {}
            if stored and row and compact_metric == stored.get("metric") and row.get("correctness") == stored.get("correctness"):
                entry = {"id": path.parent.name, "phase": "screen-pending-evidence", "candidates": [candidate],
                         "profiles": [], "artifact": str(path)}
                break
        if not entry:
            raise ValueError("evidence requires a measured snapshot")
    batch = run / "batches" / entry["id"]
    plan, result = load(batch / "campaign-plan.json", {}), load(batch / "campaign-result.json", {})
    variables = {"repo": str(REPO), "run_id": plan["run_id"], "run_dir": str(batch)}
    if campaign_fingerprint(plan, variables) != result.get("fingerprint_after"):
        raise ValueError("measured snapshot inputs changed; cannot attribute a new profile to old timings")
    return entry


def acquire(plan, run, state, baseline, candidate="current", needs=None, provider=None, *, acquisition=None, record_cost=True, surface="kernel"):
    started = time.perf_counter()
    try:
        return _acquire(plan, run, state, baseline, candidate, needs, provider,
                        acquisition=acquisition, record_cost=record_cost, surface=surface)
    except Exception as exc:
        if not limited(plan):
            raise
        cid = (state.get("best") or {}).get("id", "baseline") if candidate == "current" else candidate
        capsule = {"id": cid, "status": "blocked", "reason": "profiling-question-unresolved",
                   "unanswered": [str(exc)[:350]], "new_benchmark_runs": 0,
                   "spent_s": time.perf_counter() - started}
        if record_cost:
            event(run, "profile-only", capsule["spent_s"], candidate=cid, status="failed")
        state.setdefault("evidence_failures", {})[cid] = capsule
        save_state(run, state)
        refresh_brief(plan, run, state)
        return capsule


@bounded
def _acquire(plan, run, state, baseline, candidate="current", needs=None, provider=None, *, acquisition=None, record_cost=True, surface="kernel"):
    if not enabled(plan):
        raise ValueError("profile-only evidence requires adaptive mode")
    if candidate == "current":
        candidate = (state.get("best") or {}).get("id", "baseline")
    meta = state.get("candidates", {}).get(candidate)
    if candidate != "baseline" and (not meta or not (meta.get("result") or {}).get("correctness", {}).get("passed")):
        raise ValueError("evidence requires a correct measured candidate")
    if meta:
        entry = validate_snapshot(run, state, candidate)
    started = time.perf_counter()
    workspace = baseline if candidate == "baseline" else Path(meta["workspace"])
    parent = "baseline" if meta is None else meta["parent"]
    anchor = current_evidence(run, state, candidate_id=candidate)
    target = (anchor or {}).get("name") or (state.get("baseline_activity") or {}).get("kernel") or (
        (state.get("kernel_profiles") or {}).get("baseline") or {}).get("kernel")
    row = (meta or {}).get("result") or {"id": "baseline", "workspace": str(workspace),
                                       "pairs": 0, "correctness": {"passed": True}}
    from decision_learning import active
    from capture_history import preserve, fingerprint, reuse
    from skill_binding import binding
    key = binding(plan)["key"] if active(plan) else None
    identity = fingerprint(run, entry) if key and meta else state.get("contract_sha256")
    wanted = needs or (acquisition or {}).get("needs")
    selected_provider = provider or (acquisition or {}).get("provider")
    if candidate == "baseline" and wanted:
        from baseline_reuse import timeline
        saved = timeline(plan, run, state, wanted, selected_provider)
        if saved:
            from cupti_evidence import build
            return saved | {"agent_packet": build(plan, run, state, saved, row, evidence_only=True, delivered=False),
                            "new_benchmark_runs": 0}
    if key and identity and wanted and (acquisition is None or acquisition.get("question_origin") == "policy"):
        from evidence_scope import requires_roi
        from cupti_evidence import build
        saved = reuse(run, candidate, identity, key, wanted, selected_provider, target, requires_roi(plan))
        if saved:
            return saved | {"agent_packet": build(plan, run, state, saved, row, evidence_only=True),
                            "new_benchmark_runs": 0}
    decision = dict(acquisition or {"tool": surface, "reason": "agent-requested-capability"})
    if provider:
        decision["requested_provider"] = provider
    if acquisition is None:
        decision = route(plan, state, decision, needs)
    if decision.get("status") == "blocked":
        return {"id": candidate, "status": "blocked", "acquisition": decision, "new_benchmark_runs": 0}
    resources = collect(plan, run, workspace, candidate, parent)
    decision.update({"target_name": target, "target_anchor": candidate,
                     **capture_selector(target, resources, decision.get("tool")),
                     "target_scope": (anchor or {}).get("scope") or load(run / "profile.baseline.json", {}).get("scope")})
    if candidate == "baseline" and decision.get("tool") == "kernel":
        from kernel_runner import run_kernel_profile
        counter_plan = plan
        if limited(plan):
            counter_plan = plan | {"kernel_profile": (plan.get("kernel_profile") or {}) | {
                "bounded_question": True, "evidence_needs": decision.get("needs") or []}}
            if decision.get("target_regex"):
                counter_plan["kernel_profile"].update({
                    "baseline_kernel_regex": decision["target_regex"],
                    "kernel_name_base": decision.get("target_name_base", "demangled"),
                    "exact_kernel": False,
                })
        ncu = run_kernel_profile(counter_plan, run, "baseline", baseline, run / "profile.baseline.json", "baseline", force=False)
        state.setdefault("kernel_profiles", {})["baseline"] = ncu
        capsule = {"id": "baseline", "status": "complete", "ncu": ncu, "profile_artifact": ncu["artifact"]}
    else:
        capsule = run_iteration_profile(plan, run, candidate, row, parent,
            (state.get("kernel_profiles") or {}).get("baseline"), None, acquisition=decision)
    capsule.update({"acquisition": decision, "static": resources, "spent_s": time.perf_counter() - started})
    if capsule.get("status") == "complete":
        if key:
            capsule["capture_binding"] = key
            preserve(run, candidate, capsule, identity)
        if meta is not None:
            meta["profile"] = capsule
        else:
            state["baseline_evidence"] = capsule
        if decision.get("cost_key") and fresh_calibration(capsule):
            state.setdefault("acquisition_costs", {}).setdefault(decision["cost_key"], []).append(capsule["spent_s"])
    if record_cost:
        event(run, "profile-only", capsule["spent_s"], candidate=candidate, provider=decision.get("provider"))
    from skill_runtime import remember
    remember(plan, run, state, capsule)
    from selection_objective import reconcile
    reconcile(plan, run, state)
    save_state(run, state)
    refresh_brief(plan, run, state)
    from cupti_evidence import build
    packet = build(plan, run, state, capsule, row, evidence_only=True, delivered=record_cost)
    return capsule | {"agent_packet": packet, "new_benchmark_runs": 0}
