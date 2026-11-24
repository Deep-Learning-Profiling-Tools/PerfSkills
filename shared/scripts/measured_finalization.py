"""Publish a bounded measurement without inventing independent statistical confirmation."""

from pathlib import Path

from candidate_evidence import profile_for
from evidence_scope import requires_roi, roi_scope
from experiment_support import load, write
from experiment_state import save_state
from experiment_report import refresh_brief


def finalize(plan, run, state, baseline):
    from selection_objective import kernel_mode
    if kernel_mode(state):
        from kernel_selection_report import finalize as kernel_finalize
        return kernel_finalize(plan, run, state, baseline)
    from evidence_acquisition import validate_snapshot, acquire
    selected = state.get("selected")
    if selected:
        if selected["id"] != "baseline":
            validate_snapshot(run, state, selected["id"])
        return load(run / "measurement-verdict.json", {}) | {"cached": True, "new_benchmark_runs": 0}
    winner = (state.get("best") or {}).get("id")
    if not winner:
        raise ValueError("no measured candidate is available")
    meta = state["candidates"][winner]
    row = meta["result"]
    entry = validate_snapshot(run, state, winner)
    if (row.get("improvement") or {}).get("point", 0) <= 1:
        winner = "baseline"
        meta = {"workspace": str(baseline)}
        row = row | {"metric": row["baseline_metric"], "improvement": {"point": 1.0, "ci95": None}, "runs": 0}
    profile, path = profile_for(run, winner)
    from decision_learning import active
    from capture_history import has_activity, timing_capture
    if active(plan) and winner != "baseline":
        from skill_binding import binding
        saved = timing_capture(run, winner, entry, requires_roi(plan), binding(plan)["key"])
        profile, path = saved or ({}, path)
    timing_known = has_activity(profile, requires_roi(plan)) if active(plan) else (profile.get("totals") or {}).get("gpu_busy_s") is not None
    if not timing_known or (requires_roi(plan) and not roi_scope(profile.get("scope"))):
        needs = ["gpu-hotspot"] + (["roi-attribution"] if requires_roi(plan) else [])
        acquisition = acquire(plan, run, state, baseline, winner, needs)
        if not active(plan):
            profile, path = profile_for(run, winner)
        elif acquisition.get("status") == "complete" and acquisition.get("profile_artifact"):
            from experiment_support import REPO
            path = REPO / acquisition["profile_artifact"]
            current = load(path, {})
            profile = current if has_activity(current, requires_roi(plan)) else {}
        else:
            profile = {}
    result = {"status": "measured", "verification": "inconclusive", "campaign_complete": True,
              "next_action": "report-measured-result", "winner": winner,
              "source": meta["workspace"], "improvement": row.get("improvement"),
              "runs": row.get("runs"), "benchmark_executions": row.get("benchmark_executions"),
              "baseline_reused": row.get("baseline_reused"), "new_benchmark_runs": 0,
              "correctness": row["correctness"], "confidence": "one-or-two-unpaired-executions",
              "statistical_significance": "not-established", "profile": str(path) if profile else None,
              "profile_scope": profile.get("scope"), "timing_provider": (profile.get("tool") or {}).get("name"),
              "note": "Final selection reused measured samples. No independent final benchmark campaign was run."}
    if state.get("early_stop"):
        result["stop_reason"] = state["early_stop"]
    write(run / "measurement-verdict.json", result)
    write(run / "final-verdict.json", result | {"status": "inconclusive", "measurement_status": "complete"})
    state["selected"] = {"id": winner, "workspace": meta["workspace"], "improvement": (row.get("improvement") or {}).get("point")}
    save_state(run, state)
    refresh_brief(plan, run, state)
    return result
