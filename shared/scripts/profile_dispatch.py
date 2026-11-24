"""Dispatch a justified evidence request to its selected typed adapter."""

from pathlib import Path
from adaptive_policy import model_context
from acquisition_limits import limited
from experiment_support import candidate_dir, load


def dispatch(plan, run, candidate_id, row, parent_id, baseline_kernel_profile,
             parent_kernel_profile, force_ncu, phase, acquisition, *, locate, recapture, sections):
    tool = (acquisition or {}).get("tool", "none")
    if phase == "confirm" or (tool == "none" and not force_ncu):
        return plan, {"id": candidate_id, "status": "deferred",
                "acquisition": acquisition or {"tool": "none", "reason": "final-profiles-once"},
                "ncu": {"status": "deferred", "disposition": "deferred", "spent_s": 0},
                "models": model_context(baseline_kernel_profile)}
    if tool == "pc-sampling" and not force_ncu:
        from cupti_pc import capture
        workspace = Path(row.get("workspace") or candidate_dir(run, candidate_id) / "source")
        result = capture(plan, run, candidate_id, workspace, acquisition, validated=True)
        if result.get("status") == "complete":
            output = candidate_dir(run, candidate_id) / "profile/source-targets.json"
            raw = Path(result["profile_artifact"]).parent
            locate(run, workspace, Path(result["profile_artifact"]), output, raw)
            targets = load(output, {}).get("targets") or []
            first = next((x for x in targets if x.get("hotspot") == (result.get("target") or {}).get("name")), {})
            result["next"] = {"name": (result.get("target") or {}).get("name"),
                              "slice": (first.get("slice") or {}).get("path"), "scope": result.get("scope")}
        return plan, result
    if tool == "native" and not force_ncu:
        from native_profiler import capture
        workspace = Path(row.get("workspace") or candidate_dir(run, candidate_id) / "source")
        result = capture(plan, run, candidate_id, workspace, acquisition, validated=True)
        if result.get("status") in {"complete", "partial"}:
            output = candidate_dir(run, candidate_id) / "profile/source-targets.json"
            raw = Path(result["profile_artifact"]).parent
            locate(run, workspace, Path(result["profile_artifact"]), output, raw)
            targets = load(output, {}).get("targets") or []
            first = next((x for x in targets if x.get("hotspot") == (result.get("target") or {}).get("name")), {})
            result["next"] = {"name": (result.get("target") or {}).get("name"),
                              "slice": (first.get("slice") or {}).get("path"), "scope": result.get("scope")}
        return plan, result
    if tool == "custom" and not force_ncu:
        from custom_evidence import capture
        workspace = Path(row.get("workspace") or candidate_dir(run, candidate_id) / "source")
        result = capture(plan, run, candidate_id, workspace, acquisition)
        if result.get("status") == "complete":
            output = candidate_dir(run, candidate_id) / "profile/source-targets.json"
            raw = run / "raw/iteration" / candidate_id / acquisition["provider"]
            locate(run, workspace, Path(result["profile_artifact"]), output, raw)
            targets = load(output, {}).get("targets") or []
            first = next((x for x in targets if x.get("hotspot") == (result.get("target") or {}).get("name")), {})
            result["next"] = {"name": (result.get("target") or {}).get("name"),
                              "slice": (first.get("slice") or {}).get("path"),
                              "scope": result.get("scope")}
        return plan, result
    if tool == "activity" and not force_ncu:
        from cupti_activity import capture
        workspace = Path(row.get("workspace") or candidate_dir(run, candidate_id) / "source")
        result = capture(plan, run, candidate_id, workspace, needs=(acquisition or {}).get("needs"))
        if result.get("status") != "complete" and (plan.get("profiling") or {}).get("roi_name"):
            context = dict(acquisition or {}) | {"tool": "timeline", "provider": "pw-nsight-systems",
                "reason": "activity-ROI-unresolved-use-context", "needs": ["gpu-hotspot", "roi-attribution"]}
            fallback = recapture(plan, run, candidate_id, row, parent_id,
                baseline_kernel_profile, parent_kernel_profile, phase=phase, acquisition=context)
            fallback["acquisition"] = context
            fallback["fallback_attempts"] = [{k: result.get(k) for k in ("surface", "status", "reason", "spent_s")}]
            return plan, fallback
        if result.get("status") == "complete":
            output = candidate_dir(run, candidate_id) / "profile/source-targets.json"
            raw = run / "raw/cupti" / candidate_id
            from cupti_evidence import retain
            retain(plan, result)
            locate(run, workspace, Path(result["profile_artifact"]), output, raw)
            targets = load(output, {}).get("targets") or []
            first = next((x for x in targets if x.get("hotspot") == (result.get("target") or {}).get("name")), {})
            result["next"] = {"name": (result.get("target") or {}).get("name"),
                              "slice": (first.get("slice") or {}).get("path"),
                              "scope": result["scope"]}
        return plan, result
    # Select the evidence surface independently of sampling. Do not replay NCU
    # as a side effect of asking for a timeline, or capture a timeline for counters.
    plan = dict(plan)
    if limited(plan):
        plan["kernel_profile"] = dict(plan.get("kernel_profile") or {}) | {
            "bounded_question": not force_ncu, "evidence_needs": (acquisition or {}).get("needs") or []}
    target = (acquisition or {}).get("target_regex")
    if target and target != (plan.get("kernel_profile") or {}).get("variant_kernel_regex"):
        plan["kernel_profile"] = dict(plan.get("kernel_profile") or {}) | {"variant_kernel_regex": target}
        if tool == "kernel":
            plan["kernel_profile"].update({"kernel_name_base": (acquisition or {}).get("target_name_base", "demangled"),
                                           "exact_kernel": False})
        target_name = (acquisition or {}).get("target_name")
        baseline_name = (baseline_kernel_profile or {}).get("kernel")
        plan["adaptive_target_changed"] = bool(target_name and baseline_name and target_name != baseline_name)
    plan["iteration_profile"] = dict(plan["iteration_profile"]) | {
        "cadence": "every-candidate" if tool == "timeline" else "baseline-final"}
    plan["sampling"] = dict(plan["sampling"]) | {
        "mode": "winner-confirmation" if tool == "timeline" else "standard"}
    if tool == "kernel" and parent_kernel_profile is None:
        config = plan.get("kernel_profile") or {}
        plan["kernel_profile"] = dict(config) | {
            "candidate_sections": (config.get("section", "all") if plan.get("adaptive_target_changed")
                                   else sections(config, "variant", diagnostic=baseline_kernel_profile))}
    return plan, None
