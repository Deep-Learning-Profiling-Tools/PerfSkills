"""Publish a kernel objective with its actual timing basis and application collateral."""

from experiment_support import write
from experiment_state import save_state
from selection_objective import observation, reconcile, summary


def finalize(plan, run, state, baseline):
    from evidence_acquisition import validate_snapshot
    from experiment_report import refresh_brief
    reconcile(plan, run, state)
    unresolved = [cid for cid, m in state["candidates"].items() if m.get("result") and
                  (m["result"].get("correctness") or {}).get("passed") and
                  m.get("objective_result", {}).get("status") != "measured"]
    winner = (state.get("best") or {}).get("id", "baseline")
    if winner != "baseline":
        validate_snapshot(run, state, winner)
    measured = observation(plan, run, state, winner)
    if measured is None:
        return {"status": "inconclusive", "campaign_complete": True, "objective": summary(state),
                "winner": None, "reason": "primary-kernel-timing-unresolved",
                "missing_timing": unresolved, "new_benchmark_runs": 0}
    meta = state["candidates"].get(winner) or {"workspace": str(baseline)}
    row = meta.get("result") or {}
    ratio = (meta.get("objective_result") or {}).get("speedup", 1.)
    from objective_comparison import compare
    comparison = compare(plan, run, state, baseline, winner)
    result = {"status": "measured" if not unresolved else "inconclusive", "verification": "inconclusive",
              "campaign_complete": True, "next_action": "report-measured-result", "winner": winner,
              "source": meta["workspace"], "objective": summary(state),
              "baseline": state.get("objective_baseline"), "candidate": measured,
              "improvement": {"point": ratio, "ci95": None}, "missing_timing": unresolved,
              "correctness": row.get("correctness") or {"passed": True},
              "application_collateral": {"metric": row.get("metric"), "improvement": row.get("improvement"),
                                        "wall_improvement": row.get("wall_improvement")},
              "statistical_significance": "not-established", "new_benchmark_runs": 0}
    if comparison is not None:
        result["reference_comparison"] = comparison
        if comparison["status"] != "measured":
            result["status"] = "inconclusive"
    write(run / "measurement-verdict.json", result)
    write(run / "final-verdict.json", result | {"status": "inconclusive"})
    state["selected"] = {"id": winner, "workspace": meta["workspace"], "improvement": ratio,
                         "objective_id": state["selection_objective"]["id"]}
    save_state(run, state)
    refresh_brief(plan, run, state)
    return result
