"""Explain acquisition priority using measured bounds, never predicted speedups."""

from __future__ import annotations

import math


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def assess(plan: dict, state: dict, row: dict, acquisition: dict) -> dict:
    kernel = (state.get("kernel_profiles") or {}).get("baseline") or state.get("baseline_activity") or {}
    snapshot = "baseline"
    # A parent's counters can supersede baseline only with its own timing join.
    parent = (state.get("candidates", {}).get(row.get("id")) or {}).get("parent", "baseline")
    seen = set()
    while parent != "baseline" and parent not in seen:
        seen.add(parent)
        meta = (state.get("candidates") or {}).get(parent) or {}
        profile = meta.get("profile") or {}
        measured = profile.get("ncu") or {}
        if (profile.get("status") == "complete" and measured.get("status") in {None, "complete"}
                and measured.get("hotspot_fraction") is not None and not measured.get("target_changed")):
            kernel, snapshot = measured, parent
            break
        parent = meta.get("parent", "baseline")
    fraction = number(kernel.get("hotspot_fraction"))
    fraction = fraction if fraction is not None and 0 <= fraction <= 1 else None
    roofline = kernel.get("roofline") or {}
    ceiling = number(roofline.get("kernel_speedup_ceiling")) if roofline.get("status") == "complete" else None
    ceiling = ceiling if ceiling is not None and ceiling >= 1 else None
    anchor = state.get("evidence_anchor") or {}
    if anchor.get("target") and kernel.get("kernel") and anchor["target"] != kernel["kernel"]:
        fraction, ceiling = None, None
    recoverable = fraction * (1 - 1 / ceiling) if fraction is not None and ceiling else fraction
    roi_ceiling = 1 / (1 - recoverable) if recoverable is not None and recoverable < 1 else None
    # Unresolved questions receive full priority. Recent matching evidence is
    # deliberately discounted; this is a scheduling weight, not a probability.
    required = set(acquisition.get("needs") or [])
    uncertainty = 1.0
    for batch in (state.get("batches") or [])[-2:]:
        for candidate in batch.get("candidates", []):
            profile = ((state.get("candidates") or {}).get(candidate) or {}).get("profile") or {}
            caps = set(profile.get("observed_capabilities") or (profile.get("acquisition") or {}).get("needs") or [])
            if required and required <= caps and profile.get("status") == "complete":
                uncertainty = .25
    unit = (plan.get("metric") or {}).get("unit")
    runtime = number((row.get("metric") or {}).get("median")) if unit in {"s", "seconds"} else None
    return {"kind": "acquisition-priority-heuristic", "snapshot": snapshot,
            "freshness": "historical-anchor-not-candidate-measurement",
            "hotspot_fraction": fraction, "kernel_roofline_ceiling": ceiling,
            "roi_speedup_ceiling": roi_ceiling, "recoverable_roi_fraction_bound": recoverable,
            "bound_basis": "roofline-and-amdahl" if ceiling else "amdahl-removal" if fraction is not None else "unknown",
            "uncertainty_weight": uncertainty,
            "priority": recoverable * uncertainty if recoverable is not None else None,
            "candidate_roi_s": runtime,
            "scope": "kernel-internal-fixed-work" if ceiling else "hotspot-removal-upper-bound",
            "limitation": "Historical Roofline is not a bound on an algorithm/dataflow rewrite or a newly dominant kernel."}


def with_cost(score: dict, seconds: float | None) -> dict:
    score = dict(score)
    seconds = number(seconds)
    priority, runtime, gain = score.get("priority"), score.get("candidate_roi_s"), score.get("recoverable_roi_fraction_bound")
    score.update({"profiler_wall_s": seconds,
                  "priority_per_profiler_second": priority / seconds if priority is not None and seconds and seconds > 0 else None,
                  "best_case_amortization_executions": seconds / (runtime * gain)
                      if seconds and runtime and runtime > 0 and gain and gain > 0 else None})
    return score


def schedule(plan: dict, state: dict, row: dict, acquisition: dict) -> dict:
    if acquisition.get("tool") == "none" or acquisition.get("reason", "").startswith("agent-"):
        return acquisition
    score = assess(plan, state, row, acquisition)
    decision = acquisition | {"opportunity": score}
    settings = (plan.get("profiling") or {}).get("acquisition") or {}
    minimum = settings.get("min_hotspot_fraction", .03)
    # A low-value OLD anchor warrants a cheaper fresh timing map, never a claim
    # that the current candidate has no opportunity. Structural wins bypass this.
    small = score["hotspot_fraction"] is not None and score["hotspot_fraction"] < minimum
    threshold = (plan.get("sampling") or {}).get("practical_threshold", 1.02)
    little_headroom = score["kernel_roofline_ceiling"] is not None and score["roi_speedup_ceiling"] is not None and score["roi_speedup_ceiling"] < threshold
    refreshed = any((((state.get("candidates") or {}).get(candidate) or {}).get("profile") or {}).get("acquisition", {}).get("reason") in {
                        "small-historical-hotspot-refresh-timing-first", "roofline-headroom-small-refresh-timing-first"}
                    for batch in (state.get("batches") or [])[-2:] for candidate in batch.get("candidates", []))
    if acquisition.get("tool") == "kernel" and not refreshed and (small or little_headroom):
        decision.update({"tool": "timeline", "needs": ["gpu-hotspot", "roi-attribution"],
                         "reason": "small-historical-hotspot-refresh-timing-first" if small else
                                   "roofline-headroom-small-refresh-timing-first"})
        decision.pop("question_alternatives", None)
    return decision
