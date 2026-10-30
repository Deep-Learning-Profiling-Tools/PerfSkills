#!/usr/bin/env python3
"""Finalize from already collected paired benchmarks and candidate NCU evidence."""

from __future__ import annotations

from pathlib import Path

from experiment_report import refresh_brief
from experiment_state import save_state
from experiment_support import display, write
from terminal_view import terminal_ncu


def _eligible(state: dict, ids: list[str] | None) -> list[dict]:
    wanted = set(ids or state.get("candidates", {}))
    rows = []
    for candidate_id, item in state.get("candidates", {}).items():
        result = item.get("result") or {}
        improvement = result.get("improvement") or {}
        if (candidate_id in wanted and (result.get("correctness") or {}).get("passed")
                and improvement.get("point") is not None):
            rows.append(item)
    return sorted(rows, key=lambda item: item["result"]["improvement"]["point"],
                  reverse=True)


def promote_existing(plan: dict, run: Path, state: dict,
                     ids: list[str] | None = None) -> dict:
    """Choose the best measured candidate without new execution or profiler replay."""
    prior = state.get("confirmed")
    if prior and (not ids or prior.get("id") in ids):
        return {"status": "cached", "winner": prior["id"],
                "improvement": prior["improvement"], "evidence": prior.get("evidence"),
                "new_benchmark_runs": 0, "new_profiler_runs": 0}
    ranked = _eligible(state, ids)
    if not ranked:
        raise ValueError("no correct measured candidate is available to finalize")
    winner = ranked[0]
    result = winner["result"]
    comparison = result["improvement"]
    threshold = plan["sampling"].get("practical_threshold", 1.02)
    kernel = (winner.get("profile") or {}).get("ncu") or {}
    wall = result.get("wall_improvement") or {}
    wall_mode = plan["sampling"].get("wall_gate", "advisory")
    wall_ok = ((wall.get("ci95") or [0])[0] >= 1 / threshold)
    gates = {
        "correctness": (result.get("correctness") or {}).get("passed") is True,
        "practical_improvement": (comparison.get("ci95") or [0])[0] >= threshold,
        "candidate_kernel_evidence": bool(kernel and
            kernel.get("disposition") not in {"skipped", "failed"}),
        "no_wall_regression_over_threshold": wall_ok if wall_mode == "strict" else True,
    }
    accepted = all(gates.values())
    verdict = {
        "protocol": "profileweaver.final-verdict/v1",
        "status": "verified" if accepted else "inconclusive",
        "winner": winner["id"], "improvement": comparison,
        "application_metric": {
            "role": "primary-objective",
            "baseline": (result.get("baseline_metric") or {}).get("median"),
            "candidate": (result.get("metric") or {}).get("median"),
            "pairs": result.get("pairs"),
        },
        "kernel_profile": terminal_ncu(kernel),
        "gates": gates,
        "evidence": {
            "mode": "reuse-screening-and-candidate-profile",
            "new_benchmark_runs": 0, "new_profiler_runs": 0,
            "note": ("Paired application measurements and candidate NCU evidence were already "
                     "collected during evaluation; no post-budget replay was performed."),
            "profile_delta": (winner.get("profile") or {}).get("artifact"),
        },
    }
    write(run / "final-verdict.json", verdict)
    state["last_confirmation"] = {"winner": winner["id"], "accepted": accepted,
                                  "gates": gates, "mode": verdict["evidence"]["mode"]}
    if accepted:
        state["confirmed"] = {
            "id": winner["id"], "improvement": comparison["point"],
            "workspace": winner["workspace"], "evidence": verdict["evidence"]["mode"],
        }
    save_state(run, state)
    refresh_brief(plan, run, state)
    return {
        "status": "complete" if accepted else "inconclusive",
        "winner": winner["id"], "improvement": comparison["point"],
        "gates": gates, "benchmark_pairs_reused": result.get("pairs"),
        "ncu": terminal_ncu(kernel), "new_benchmark_runs": 0, "new_profiler_runs": 0,
        "artifact": display(run / "final-verdict.json"),
    }
