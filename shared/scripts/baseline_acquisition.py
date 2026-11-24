"""Choose baseline evidence by capability; no compulsory counter replay."""

import shutil
import time
from evidence_router import route
from evidence_scope import requires_roi
from experiment_support import load, write
from baseline_control import adaptive, capture_timeout, progress
from acquisition_limits import bounded


@bounded
def acquire(plan, run, workspace, profile, sources, raw, stages):
    needs = list((plan.get("profiling") or {}).get("baseline_needs") or ["gpu-hotspot"])
    if requires_roi(plan) and "roi-attribution" not in needs:
        needs.append("roi-attribution")
    selected = route(plan, {}, {"tool": "timeline", "reason": "baseline-missing-observables", "question_origin": "policy"}, needs)
    disposition = {"needs": needs, "selected": selected, "attempts": []}
    progress(plan, run, "profiler-selection", "complete", provider=selected.get("provider"),
             unavailable=selected.get("unavailable", []))
    write(run / "baseline-acquisition.json", disposition)
    capsule = None
    started = time.perf_counter()
    if selected.get("tool") == "activity":
        from cupti_activity import capture
        bounded = plan
        if adaptive(plan):
            bounded = plan | {"execution": plan["execution"] | {"timeout_s": capture_timeout(plan, run)}}
        progress(plan, run, "activity-capture", "running")
        try:
            capsule = capture(bounded, run, "baseline", workspace, needs=needs)
        except Exception as exc:
            disposition["attempts"].append({"surface": "pw-cupti", "status": "failed", "reason": str(exc),
                                           "spent_s": time.perf_counter() - started})
            write(run / "baseline-acquisition.json", disposition)
            progress(plan, run, "activity-capture", "failed", reason=str(exc))
            raise
        progress(plan, run, "activity-capture", capsule.get("status"), elapsed_s=capsule.get("spent_s"))
    elif selected.get("tool") == "native":
        from native_profiler import capture
        capsule = capture(plan, run, "baseline", workspace, selected, validated=True)
    if capsule:
        disposition["attempts"].append({k: capsule.get(k) for k in ("surface", "status", "reason", "spent_s", "scope")})
        # This component belongs to initialize's inclusive cost; do not double count.
        if capsule.get("status") == "complete":
            shutil.copy2(capsule["profile_artifact"], profile)
            from iteration_profile import _locate_source
            _locate_source(run, workspace, profile, sources, raw)
            disposition["provider"] = selected["provider"]
            disposition["diagnostic_metric"] = capsule.get("diagnostic_metric")
            disposition["diagnostic_metric_role"] = "instrumented-not-for-ranking"
            disposition["adapter_wall_s"] = time.perf_counter() - started
            disposition["cost_scope"] = "capture-reduction-and-source-location"
            write(run / "baseline-acquisition.json", disposition)
            from skill_runtime import remember
            remember(plan, run, {}, capsule | {"id": "baseline", "acquisition": selected,
                     "spent_s": disposition["adapter_wall_s"]})
            return []
    # Host waits/overlap/sync, unsupported ROI or unavailable collector need context.
    # Retain the declared timeline stages; never silently reuse whole-process data as ROI.
    context = route(plan, {}, {"tool": "timeline", "reason": "baseline-capability-fallback", "question_origin": "policy",
                              "requested_provider": "pw-nsight-systems"}, needs)
    if context.get("status") == "blocked":
        write(run / "baseline-acquisition.json", disposition | {"fallback": context})
        raise RuntimeError("no capable baseline collector for requested observables")
    disposition.update({"provider": "pw-nsight-systems", "fallback": (capsule or {}).get("reason"),
                        "reason": "requested-context-or-lightweight-capability-unresolved"})
    write(run / "baseline-acquisition.json", disposition)
    return stages
