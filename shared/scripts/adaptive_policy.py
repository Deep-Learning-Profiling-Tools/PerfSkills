"""Bounded screening and evidence acquisition; legacy contracts stay unchanged.

These are acquisition decisions, not optimization suggestions or speedup predictions.
Static resources and analytical ceilings never stand in for measured performance.
"""

from __future__ import annotations

import math


def enabled(plan: dict) -> bool:
    return (plan.get("sampling") or {}).get("mode") == "adaptive"


def configure(sampling: dict) -> dict:
    return {"warmup": 1, "min_pairs": 5, "max_pairs": 7,
            "final_min_pairs": 5, "final_max_pairs": 9, "finalists": 2,
            "bootstrap_iterations": 1000, "practical_threshold": 1.02,
            "wall_gate": "advisory", **sampling, "mode": "adaptive",
            "finalists": sampling.get("adaptive_finalists", 1),
            "final_min_pairs": sampling.get("adaptive_final_min_pairs", 3),
            "final_max_pairs": sampling.get("adaptive_final_max_pairs", 5),
            "confirmation_policy": "bounded-v2",
            "screen_warmup": sampling.get("screen_warmup", 1),
            "screen_warmup_auto": sampling.get("screen_warmup_auto", "screen_warmup" not in sampling),
            "screen_min_pairs": 1, "screen_max_pairs": 2}


def bounded_executions(sampling):
    """New-run budget counts executions, not interleaved pairs."""
    return sampling | {"measurement_policy": "single-or-two-v3",
                       "benchmark_max_runs": sampling.get("benchmark_max_runs", 1),
                       "screen_warmup": 0, "screen_warmup_auto": False, "finalists": 1,
                       "min_pairs": 0, "max_pairs": 0, "screen_min_pairs": 0, "screen_max_pairs": 0,
                       "final_min_pairs": 0, "final_max_pairs": 0, "final_confirmation": "reuse-existing-samples"}


def needs_second_pair(base: list[float], candidate: list[float], direction: str,
                      sampling: dict) -> bool:
    """Spend a second pair near the incumbent, near a tie, or at timer resolution."""
    b, c = base[-1], candidate[-1]
    if not all(math.isfinite(x) and x > 0 for x in (b, c)):
        return True
    ratio = b / c if direction == "lower" else c / b
    margin = sampling.get("screen_margin", .05)
    incumbent = sampling.get("incumbent_speedup", 1.0)
    near = abs(ratio / incumbent - 1) <= margin or abs(ratio - 1) <= margin
    resolution = sampling.get("metric_resolution", 0)
    unresolved = resolution > 0 and min(b, c) <= 20 * resolution
    reference = sampling.get("screen_baseline_reference")
    drift = reference and abs(b / reference - 1) > margin
    return bool(near or unresolved or drift)


def select_evidence(plan: dict, state: dict, row: dict, resources: dict,
                    request: str = "auto") -> dict:
    from evidence_opportunity import schedule
    return schedule(plan, state, row, _select_evidence(plan, state, row, resources, request))


def _select_evidence(plan: dict, state: dict, row: dict, resources: dict,
                     request: str = "auto") -> dict:
    """Acquire the cheapest evidence that answers the currently observed question."""
    if request != "auto":
        return {"tool": request, "reason": "agent-requested", "screening_only": True}
    if not (row.get("correctness") or {}).get("passed"):
        return {"tool": "none", "reason": "correctness-failed", "screening_only": True}
    measured = sum(bool(x.get("result")) for x in state.get("candidates", {}).values())
    budget = (plan.get("budget") or {}).get("iterations")
    if budget and measured >= budget:
        return {"tool": "none", "reason": "reserve-evidence-for-final-winner", "screening_only": True}
    if resources.get("new_kernel_bases"):
        return {"tool": "timeline", "reason": "new-kernel-execution-identity-unresolved", "screening_only": True}
    if ((plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3" and
            (row.get("ranking") or {}).get("status") in {"unstable", "invalid", "resolution-limited"}):
        return {"tool": "none", "reason": "uncertain-timing-does-not-justify-counters", "screening_only": True}
    sampling = plan.get("sampling") or {}
    threshold = sampling.get("practical_threshold", 1.02)
    speedup = (row.get("improvement") or {}).get("point", 1)
    wall = (row.get("wall_improvement") or {}).get("point", 1)
    wall_limit = threshold if sampling.get("wall_gate") == "strict" else 1.10
    wall_high = ((row.get("wall_improvement") or {}).get("ci95") or [wall, wall])[1]
    wall_resolved = (row.get("pairs", 0) >= 2 and wall_high < 1 / wall_limit) or wall <= .80
    if speedup >= threshold and wall_resolved:
        return {"tool": "timeline", "reason": "roi-wall-disagreement", "screening_only": True}
    changes = resources.get("delta_parent", resources.get("delta_baseline")) or []
    if any((x.get("local_bytes") or 0) > 0 or (x.get("stack_bytes") or 0) > 0
           or (x.get("registers") or 0) >= 16 for x in changes):
        return {"tool": "kernel", "reason": "static-resource-growth", "screening_only": True}
    # Age only profiles actually acquired along the campaign, not a repeated request
    # for the same candidate. Skipped evidence must never masquerade as a fresh profile.
    previous = [x for x in state.get("batches", []) if x.get("phase") == "screen"]
    prior_ids = previous[-1].get("candidates", []) if previous else []
    prior = ((state.get("candidates", {}).get(prior_ids[0]) or {}).get("result") or {}) if prior_ids else {}
    from selection_objective import policy_row, kernel_mode
    if prior and kernel_mode(state):
        prior = policy_row(state,prior)
    prior_speedup = (prior.get("improvement") or {}).get("point")
    incumbent = (state.get("best") or {}).get("improvement", 1.0)
    from decision_learning import active
    if active(plan):
        incumbent = (row.get("ranking") or {}).get("incumbent_at_screen") or incumbent
    prior_incumbent = ((prior.get("ranking") or {}).get("incumbent_at_screen") or
                       (previous[-1].get("incumbent_before") if previous else None))
    from capture_reuse import retained_mechanism
    recent_mechanism = retained_mechanism(plan, state)
    if (prior_speedup is not None and prior_incumbent is not None
            and prior_speedup < prior_incumbent * threshold
            and speedup < incumbent * threshold):
        if recent_mechanism:
            return {"tool": "none", "reason": "recent-mechanism-evidence-still-valid", "screening_only": True}
        baseline = (state.get("kernel_profiles") or {}).get("baseline") or {}
        diagnosis = baseline.get("advice") or {}
        family = (diagnosis.get("primary") or {}).get("name", "")
        needs = (["memory-latency", "instruction-stalls"] if "memory" in family else
                 ["warp-divergence", "instruction-stalls"] if "divergence" in family else
                 ["gpu-occupancy", "register-pressure"] if "register" in family else
                 ["instruction-stalls"])
        questions = ([needs] if any(x in family for x in ("divergence", "register")) else
                     [["causal-stall-attribution"], ["gpu-pc", "instruction-stalls"], needs]
                     if "memory" in family else [["instruction-stalls"]])
        if "memory" in family and "gvprof" in ((plan.get("profiling") or {}).get("artifacts") or []):
            questions.insert(0, ["value-redundancy"])
        return {"tool": "kernel", "needs": needs,
                "question_alternatives": questions,
                "reason": "plateau-needs-mechanism-evidence", "screening_only": True}
    age = 0
    for batch in reversed(previous):
        if any(x.get("status") == "complete" for x in batch.get("profiles", [])):
            break
        age += 1
    anchor = state.get("evidence_anchor_speedup", 1.0)
    if speedup / anchor >= 1.20:
        return {"tool": "timeline", "reason": "large-gain-remap-hotspots", "screening_only": True}
    if age >= 2 and abs(speedup / anchor - 1) >= .10:
        return {"tool": "timeline", "reason": "aged-evidence-with-runtime-movement", "screening_only": True}
    return {"tool": "none", "reason": "timing-and-static-evidence-sufficient",
            "screening_only": True, "evidence_age": age + 1}


def model_context(kernel: dict | None) -> dict:
    """Expose measured Roofline/Amdahl bounds, without predicting a candidate win."""
    kernel = kernel or {}
    fraction = kernel.get("hotspot_fraction")
    ceiling = 1 / (1 - fraction) if isinstance(fraction, (int, float)) and 0 <= fraction < 1 else None
    return {"hotspot_fraction": fraction, "amdahl_ceiling": ceiling,
            "scope": "baseline-anchor", "candidate_prediction": None}


def record_evidence_anchor(state: dict, acquisition: dict, capsule: dict, row: dict) -> None:
    """Any capable successful hotspot provider can refresh the timing anchor."""
    target = (capsule.get("next") or {}).get("name") or (capsule.get("target") or {}).get("name")
    if (capsule.get("status") == "complete" and target and
            ("observed_capabilities" not in capsule or "gpu-hotspot" in capsule["observed_capabilities"]) and
            "gpu-hotspot" in (acquisition.get("needs") or [])):
        state["evidence_anchor_speedup"] = (row.get("improvement") or {}).get("point", 1.0)
        state["evidence_anchor"] = {"candidate": capsule.get("id"),
                                    "provider": acquisition.get("provider"), "target": target}


def warmup_for_profile(sampling: dict, baseline_profile: dict) -> tuple[int, dict | None]:
    count = sampling.get("screen_warmup", 1)
    if not sampling.get("screen_warmup_auto"):
        return count, None
    duration = (baseline_profile.get("totals") or {}).get("wall_time_s")
    maximum = sampling.get("automatic_warmup_max_roi_s", 1.0)
    if isinstance(duration, (int, float)) and duration >= maximum:
        return 0, {"reason": "long-baseline-ROI-avoid-extra-execution", "profiled_roi_s": duration}
    return count, {"reason": "short-or-unknown-baseline-ROI-first-use-guard", "profiled_roi_s": duration}
