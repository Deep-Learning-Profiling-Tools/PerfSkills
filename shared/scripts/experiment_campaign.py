#!/usr/bin/env python3
"""Screen candidate batches, confirm winners, and run final reprofiling."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from candidate_workspace import campaign_plan, compact_candidate
from brief_facts import classify_iteration, source_features
from experiment_fresh import run_final_stages
from experiment_report import refresh_brief
from experiment_state import save_state
from experiment_support import REPO, load, write
from iteration_profile import run_iteration_profile
from kernel_evidence import attach_advice
from terminal_view import terminal_ncu, terminal_profile


def run_campaign(plan: dict, plan_path: Path, run: Path, state: dict,
                 baseline: Path, ids: list[str], phase: str,
                 diagnose_ids: list[str] | None = None) -> dict:
    if len(ids) != 1:
        raise ValueError("evaluate/confirm requires exactly one candidate id")
    force_ncu = set(diagnose_ids or [])
    unknown_diagnose = force_ncu - set(ids)
    if unknown_diagnose:
        raise ValueError(f"--ncu candidates are not in this batch: {','.join(sorted(unknown_diagnose))}")
    if force_ncu and not (plan.get("kernel_profile") or {}).get("enabled"):
        raise ValueError("--ncu requires kernel_profile.enabled=true in the experiment plan")
    sequence = len(state["batches"]) + 1
    name = (f"screen-{sequence:02d}" if phase == "screen" else f"final-{sequence:02d}")
    batch = run / "batches" / name
    batch.mkdir(parents=True, exist_ok=True)
    generated = campaign_plan(plan, run, baseline, state, ids, phase, batch)
    generated_path = batch / "campaign-plan.json"
    write(generated_path, generated)
    raw = run / "raw/experiment"
    raw.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, str(REPO / "shared/scripts/campaign.py"), str(generated_path)],
                          cwd=REPO, capture_output=True, text=True)
    (raw / f"{name}.stdout.txt").write_text(proc.stdout)
    (raw / f"{name}.stderr.txt").write_text(proc.stderr)
    result = load(batch / "campaign-result.json", {})
    if proc.returncode or result.get("status") != "complete":
        raise RuntimeError(f"{phase} campaign failed; see {raw / f'{name}.stderr.txt'}")
    rows = [compact_candidate(row) for row in result["candidates"]]
    accepted = phase != "confirm" or all(result["final"].get("gates", {}).values())
    by_id = {row["id"]: row for row in rows}
    for candidate_id, row in by_id.items():
        state["candidates"][candidate_id]["result"] = row
        state["candidates"][candidate_id]["status"] = (
            "screened" if phase == "screen" or not accepted else "confirmed")
    profiles = []
    baseline_kernel = (state.get("kernel_profiles") or {}).get("baseline")
    for candidate_id in ids:
        parent_id = state["candidates"][candidate_id]["parent"]
        parent_kernel = (baseline_kernel if parent_id == "baseline" else
                         ((state["candidates"].get(parent_id) or {}).get("profile") or {}).get("ncu"))
        capsule = run_iteration_profile(
            plan, run, candidate_id, by_id[candidate_id], parent_id,
            baseline_kernel, parent_kernel, force_ncu=candidate_id in force_ncu,
            phase=phase)
        history = Counter()
        for meta in state["candidates"].values():
            if meta.get("result"):
                for mechanism in classify_iteration(meta):
                    history[mechanism] += 1
        observed = max(((meta.get("result") or {}).get("improvement") or {}).get("point", 0)
                       for meta in state["candidates"].values())
        if capsule.get("ncu") and capsule["ncu"].get("status") in {None, "complete"}:
            ncu = capsule["ncu"]
            target_doc = load(run / "candidates" / candidate_id / "profile" / "source-targets.json", {})
            features, _ = source_features(run, (target_doc.get("targets") or [])[:7])
            profile_doc = load(run / "candidates" / candidate_id / "profile" / "profile.json", {})
            scope = profile_doc.get("scope") or {}
            pre_copy = ((scope.get("copies") or {}).get("before_roi") or {})
            if (pre_copy.get("union_s") or 0) > 0:
                metric_name = str((plan.get("metric") or {}).get("name") or "").lower()
                features.append("setup-transfer-heavy" if any(
                    token in metric_name for token in ("whole", "wall", "end-to-end", "total"))
                    else "setup-transfer-collateral")
            capsule["ncu"] = attach_advice(
                ncu, ncu.get("hotspot_fraction"), dict(history), observed, features)
        state["candidates"][candidate_id]["profile"] = capsule
        profiles.append(capsule)
    winner = result["final"]["winner"]
    point = result["final"]["baseline_vs_winner"]["point"]
    winner_profile = next((item for item in profiles if item["id"] == winner), None)
    timed_profiles = [item for item in profiles
                      if item.get("status") == "complete" and
                      isinstance(((item.get("ncu") or {}).get("duration_ns")), (int, float))]
    kernel_winner = min(timed_profiles, key=lambda item: item["ncu"]["duration_ns"],
                        default=winner_profile)
    if kernel_winner and kernel_winner.get("status") == "complete":
        kernel_ncu = kernel_winner.get("ncu") or {}
        baseline_duration = (baseline_kernel or {}).get("duration_ns")
        duration = kernel_ncu.get("duration_ns")
        kernel_speedup = (baseline_duration / duration if baseline_duration and duration else None)
        prior = state.get("best_kernel") or {}
        prior_duration = prior.get("duration_ns")
        tied_but_better_roi = (duration is not None and duration == prior_duration and
                               point > (prior.get("roi_improvement") or 0))
        if (prior_duration is None or
                (duration is not None and duration < prior_duration) or tied_but_better_roi):
            state["profile_frontier"] = {
                "candidate": kernel_winner["id"], "target": kernel_winner.get("target"),
                "next": kernel_winner.get("next"), "ncu_advice": kernel_ncu.get("advice"),
                "kernel_duration_ns": duration, "kernel_speedup": kernel_speedup,
                "artifact": kernel_winner.get("artifact")}
            state["best_kernel"] = {"id": kernel_winner["id"], "duration_ns": duration,
                                    "speedup": kernel_speedup, "roi_improvement": point}
    state["batches"].append({"id": name, "phase": phase, "candidates": ids,
                             "winner": winner, "improvement": point,
                             "profiles": [{"id": item["id"], "status": item["status"],
                                           "artifact": item.get("artifact")} for item in profiles],
                             "artifact": str(batch / "campaign-result.json")})
    if not state.get("best") or point > state["best"]["improvement"]:
        state["best"] = {"id": winner, "improvement": point,
                         "workspace": state["candidates"][winner]["workspace"]}
    if phase == "confirm":
        shutil.copy2(generated_path, run / "campaign-plan.json")
        shutil.copy2(batch / "campaign-result.json", run / "campaign-result.json")
        shutil.copy2(batch / "campaign-brief.txt", run / "campaign-brief.txt")
        state["last_confirmation"] = {"winner": winner, "accepted": accepted,
                                      "gates": result["final"].get("gates", {})}
        if accepted:
            state["confirmed"] = {"id": winner, "improvement": point,
                                  "workspace": state["candidates"][winner]["workspace"]}
            # The finalizer reads experiment-state.json. Persist confirmation before invoking it.
            save_state(run, state)
            run_final_stages(plan, run, state, result)
    save_state(run, state)
    refresh_brief(plan, run, state)
    automatic = None
    measured = sum(1 for item in state["candidates"].values() if item.get("result"))
    budget = (plan.get("budget") or {}).get("iterations")
    if phase == "screen" and budget and measured >= budget and not state.get("confirmed"):
        if (plan.get("sampling") or {}).get("mode") == "winner-confirmation":
            winner_id = (state.get("best") or {}).get("id")
            if not winner_id:
                raise RuntimeError("winner-confirmation mode has no measured winner")
            save_state(run, state)
            automatic = {"mode": "winner-only-confirmation",
                         "result": run_campaign(plan, plan_path, run, state, baseline,
                                                [winner_id], "confirm")}
        else:
            from experiment_finalize import promote_existing
            automatic = promote_existing(plan, run, state)
    return {"status": "complete" if accepted else "inconclusive", "phase": phase,
            "accepted": accepted, "winner": winner, "improvement": point,
            "kernel_winner": state.get("best_kernel"),
            "pairs": result["final"]["pairs"],
            "profiles": [_terminal_candidate(item, by_id[item["id"]],
                                             confirmation=phase == "confirm")
                         for item in profiles], "batch": name,
            **({"automatic_finalization": automatic} if automatic else {}),
            **({"final_ncu": terminal_ncu((state.get("kernel_profiles") or {}).get("final"))}
               if accepted and (state.get("kernel_profiles") or {}).get("final") else {})}


def _terminal_candidate(profile: dict, result: dict, confirmation=False) -> dict:
    view = terminal_profile(profile, confirmation=confirmation)
    improvement = result.get("improvement") or {}
    view["roi"] = {
        "baseline": (result.get("baseline_metric") or {}).get("median"),
        "candidate": (result.get("metric") or {}).get("median"),
        "speedup": improvement.get("point"), "ci95": improvement.get("ci95"),
        "pairs": result.get("pairs"),
        "correct": (result.get("correctness") or {}).get("passed"),
        "role": "primary-objective",
    }
    if view.get("ncu"):
        view["ncu"]["role"] = "mechanism-evidence"
    return view
