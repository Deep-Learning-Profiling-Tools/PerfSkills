"""Avoid discretionary replay when recent evidence or a measured loss already answers the step."""

from acquisition_limits import remaining, AcquisitionTimeout
from diagnostic_context import refined
from experiment_support import write


def retained_mechanism(plan, state):
    """Evidence expires with its retained snapshot, not after a fixed number of iterations."""
    if not refined(plan):
        for batch in state.get("batches", [])[-2:]:
            ids = set(batch.get("candidates", [])) | {p["id"] for p in batch.get("profiles", []) if p.get("id")}
            for cid in ids:
                capsule = (state.get("candidates", {}).get(cid) or {}).get("profile") or {}
                if capsule.get("status") == "complete" and set((capsule.get("acquisition") or {}).get("needs") or []) & {
                        "instruction-stalls", "memory-latency", "gpu-occupancy", "causal-stall-attribution"}:
                    return True
        return False
    from pathlib import Path
    from skill_experience import observed
    target = (state.get("best") or {}).get("id", "baseline")
    meta = state.get("candidates", {}).get(target) or {}
    capsule = ((state.get("baseline_evidence") or {}) if target == "baseline" else meta.get("profile") or {})
    known = observed(capsule) if capsule.get("status") == "complete" else set()
    if plan.get("run_dir"):
        from capture_history import known_capabilities
        known.update(known_capabilities(Path(plan["run_dir"]), state, target))
    return bool(known & {"instruction-stalls", "memory-latency", "gpu-occupancy", "causal-stall-attribution"})


def remaining_time():
    """An expired discretionary planning budget means edit/reuse, not a new capture."""
    try:
        return remaining()
    except AcquisitionTimeout:
        return 0.


def record(plan, run, state, cid, trigger, selected):
    write(run / "candidates" / cid / "acquisition-decision.json", {
        "policy": plan["profiling"]["skill_runtime"]["decision_policy"], "trigger": trigger,
        "snapshot": (state.get("best") or {}).get("id", "baseline"), "selected": selected,
        "options": [], "score_role": "reuse-current-facts-before-discretionary-replay"})


def before_optional(plan, state, row, decision):
    if not refined(plan):
        return None
    hypothesis = (state.get("candidates", {}).get(row["id"], {}).get("hypothesis") or {})
    if hypothesis.get("alternatives"):
        return None  # A named missing discriminator still competes on capability and cost.
    reason = decision.get("reason")
    if reason == "recent-mechanism-evidence-still-valid":
        return {"tool": "none", "reason": reason, "action": "compare-source-hypotheses",
                "screening_only": True}
    point = (row.get("improvement") or {}).get("point")
    before = (row.get("ranking") or {}).get("incumbent_at_screen") or 1.
    quality = (row.get("ranking") or {}).get("status")
    if (reason == "static-resource-growth" and point is not None and point < max(1., before) and
            (row.get("correctness") or {}).get("passed") and quality not in
            {"unstable", "invalid", "resolution-limited"}):
        return {"tool": "none", "reason": "measured-regression-static-resources-available",
                "action": "compare-source-hypotheses", "screening_only": True}
    return None
