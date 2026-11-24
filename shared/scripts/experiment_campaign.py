#!/usr/bin/env python3
"""Coordinate candidate measurement, optional evidence and result publication."""

from pathlib import Path
import subprocess
from adaptive_policy import enabled, select_evidence
from compiler_evidence import collect as compiler_evidence
from evidence_router import route as route_evidence
from experiment_fresh import run_final_stages
from experiment_report import refresh_brief
from iteration_profile import run_iteration_profile
from terminal_view import terminal_profile
from campaign_measurement import measure_candidates
from campaign_profiles import profile_candidates
from campaign_publication import publish
from selection_objective import notice
from screen_outcome import terminal as screen_outcome


def run_campaign(plan: dict, plan_path: Path, run: Path, state: dict,
                 baseline: Path, ids: list[str], phase: str,
                 diagnose_ids: list[str] | None = None, evidence: str = "auto",
                 needs: list[str] | None = None, provider: str | None = None, retry_evidence=False) -> dict:
    if phase == "confirm" and (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3":
        from measured_finalization import finalize
        return finalize(plan, run, state, baseline) | {"requested_candidates": ids, "new_comparison_runs": 0}
    if (phase == "screen" and len(ids) == 1 and
            (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3"):
        meta = state.get("candidates", {}).get(ids[0]) or {}
        if meta.get("result"):
            from evidence_acquisition import validate_snapshot
            try:
                entry = validate_snapshot(run, state, ids[0])
            except ValueError:
                pass
            else:
                row = meta["result"]
                from measured_checkpoint import recover
                from shared_activity_measurement import resume
                resume(run,state,ids[0],entry)
                recover(plan, run, state, ids[0], entry)
                refresh_brief(plan, run, state)
                if needs or provider or evidence not in {"auto", "none"} or diagnose_ids:
                    from evidence_acquisition import acquire
                    capsule = acquire(plan, run, state, baseline, ids[0], needs, provider,
                                      surface="timeline" if evidence == "timeline" else "kernel")
                    return {"status": "cached", "candidate": ids[0], "new_benchmark_runs": 0,
                            "improvement": row.get("improvement"), "evidence": capsule.get("agent_packet"), **notice(state),
                            **screen_outcome(plan, state, ids[0], True)}
                budget = (plan.get("budget") or {}).get("iterations")
                measured = sum(bool(m.get("result")) for m in state["candidates"].values())
                if budget and (measured >= budget or state.get("early_stop")) and not state.get("selected"):
                    from measured_finalization import finalize
                    return finalize(plan, run, state, baseline)
                return {"status": "cached", "candidate": ids[0], "new_benchmark_runs": 0,
                        "runs": row.get("runs"), "improvement": row.get("improvement"),
                        "campaign_complete": bool(state.get("selected")),
                        "next_action": "report-measured-result" if state.get("selected") else "continue-search", **notice(state),
                        **screen_outcome(plan, state, ids[0], True)}
    if phase == "screen" and state.get("early_stop"):
        raise ValueError("flexible campaign has closed: " + state["early_stop"])
    if enabled(plan) and phase == "confirm" and state.get("confirmed"):
        from confirmation_publication import protect
        published = dict(state["confirmed"])
        with protect(run, state) as publication:
            result = _run_campaign(plan, plan_path, run, state, baseline, ids, phase,
                                   diagnose_ids, evidence, needs, provider, retry_evidence)
            publication["commit"] = bool(result.get("accepted"))
        if not result.get("accepted"):
            result.update({"published_winner": published["id"], "published_status": "verified",
                           "publication": "previous-verification-preserved"})
        return result
    return _run_campaign(plan, plan_path, run, state, baseline, ids, phase,
                         diagnose_ids, evidence, needs, provider, retry_evidence)


def _run_campaign(plan, plan_path, run, state, baseline, ids, phase, diagnose_ids=None,
                  evidence="auto", needs=None, provider=None, retry_evidence=False):
    session = measure_candidates(plan, plan_path, run, state, baseline, ids, phase,
                                 diagnose_ids, evidence, needs, provider, retry_evidence)
    from selection_objective import kernel_mode
    if phase == "screen" and kernel_mode(state):
        from objective_acquisition import ensure
        for cid in ids:
            ensure(plan, run, state, baseline, cid)
    profiles, references = profile_candidates(plan, run, state, baseline, ids, phase, session,
        evidence, needs, provider, compiler_evidence=compiler_evidence, select_evidence=select_evidence,
        route_evidence=route_evidence, run_iteration_profile=run_iteration_profile)
    return publish(plan, plan_path, run, state, baseline, ids, phase, session, profiles, references,
        retry_evidence, run_campaign=run_campaign, run_final_stages=run_final_stages,
        refresh_brief=refresh_brief, terminal_candidate=_terminal_candidate)


def _terminal_candidate(profile: dict, result: dict, confirmation=False) -> dict:
    if profile.get("agent_packet"):
        return profile["agent_packet"]
    view = terminal_profile(profile, confirmation=confirmation)
    improvement = result.get("improvement") or {}
    view["roi"] = {
        "baseline": (result.get("baseline_metric") or {}).get("median"),
        "candidate": (result.get("metric") or {}).get("median"),
        "speedup": improvement.get("point"), "ci95": improvement.get("ci95"),
        "pairs": result.get("pairs"),
        "correct": (result.get("correctness") or {}).get("passed"),
        "role": "primary-objective",
        **({"ranking": result["ranking"]} if result.get("ranking") else {}),
    }
    wall = result.get("wall_improvement") or {}
    if (wall.get("point") or 1) < .98:
        view["whole_process"] = {"speedup": wall.get("point"), "ci95": wall.get("ci95")}
    if view.get("ncu"):
        view["ncu"]["role"] = "mechanism-evidence"
    return view
