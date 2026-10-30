#!/usr/bin/env python3
"""Single backend campaign CLI used only by experiment.py."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from campaign_io import (REPO, campaign_fingerprint, load, resolve, validate_plan, write)
from campaign_run import evaluate_candidate, run_build
from campaign_select import final_confirmation, screening_decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    plan = load(args.plan.resolve())
    if not plan or plan.get("version") != 1:
        parser.error("campaign plan must be JSON with version=1")
    validate_plan(plan)
    variables = {"repo": str(REPO), "run_id": plan["run_id"]}
    run_dir = resolve(plan["run_dir"], variables)
    run_dir.mkdir(parents=True, exist_ok=True)
    variables["run_dir"] = str(run_dir)
    fingerprint = campaign_fingerprint(plan, variables)
    result_path = run_dir / "campaign-result.json"
    prior = load(result_path, {})
    if prior.get("status") == "complete" and prior.get("fingerprint") == fingerprint:
        print(json.dumps({"status": "cached", "run_id": plan["run_id"],
                          "winner": (prior.get("final") or {}).get("winner"),
                          "result": str(result_path), "brief": str(run_dir / "campaign-brief.txt")},
                         separators=(",", ":")))
        return 0
    started = time.perf_counter()
    baseline, candidates = plan["baseline"], plan["candidates"]
    if len(candidates) != 1:
        raise ValueError("campaign plans must contain exactly one candidate")
    try:
        phase = plan.get("phase", "confirm")
        builds = {baseline["id"]: run_build(baseline, plan, run_dir, variables)}
        results = []
        for index, candidate in enumerate(candidates, 1):
            builds[candidate["id"]] = run_build(candidate, plan, run_dir, variables)
            if phase != "final-only":
                results.append(evaluate_candidate(baseline, candidate, plan, run_dir, variables, index))
        if phase == "final-only":
            prior = {item["id"]: item for item in plan["screening"]}
            results = [prior[item["id"]] for item in candidates]
        final = (screening_decision(results, plan) if phase == "screen" else
                 final_confirmation(baseline, candidates, results, plan, run_dir, variables))
        fingerprint_after = campaign_fingerprint(plan, variables)
        if fingerprint_after != fingerprint:
            raise RuntimeError("declared campaign inputs changed during execution")
        result = {"protocol": "profileweaver.campaign-result/v1", "status": "complete",
                  "run_id": plan["run_id"], "fingerprint": fingerprint,
                  "fingerprint_after": fingerprint_after,
                  "generated_utc": datetime.now(timezone.utc).isoformat(),
                  "elapsed_s": time.perf_counter() - started, "phase": phase,
                  "metric": plan["metric"],
                  "sampling": plan["sampling"], "builds": builds, "candidates": results,
                  "final": final}
        if phase == "screen" and any(item["pairs"] < 2 for item in results):
            result["confidence"] = "screening-only-single-pass"
        write(result_path, result)
        iterations = []
        for index, item in enumerate(results, 1):
            primary = {"median": item["metric"]["median"], "unit": plan["metric"].get("unit"),
                       "name": plan["metric"].get("name"),
                       "improvement": (item.get("improvement") or {}).get("point")}
            entry = {"iteration": index, "optimization": item.get("description") or item["id"],
                     "mechanism": item.get("mechanism"),
                     "result": {"primary": primary, "correctness": item["correctness"]}}
            if plan["metric"].get("unit") in {"s", "sec", "second", "seconds"}:
                entry["result"]["roi"] = {"median_s": primary["median"],
                                            "speedup": primary["improvement"]}
            iterations.append(entry)
        write(run_dir / "iterations.json", iterations)
        winner = next(item for item in results if item["id"] == final["winner"])
        confidence = result.get("confidence", "paired-bootstrap")
        brief = (f"ProfileWeaver campaign {phase}; confidence={confidence}; candidates={len(results)}; winner={final['winner']}; "
                 f"improvement={final['baseline_vs_winner']['point']:.3f}x "
                 f"CI=[{final['baseline_vs_winner']['ci95'][0]:.3f},{final['baseline_vs_winner']['ci95'][1]:.3f}]; "
                 f"pairs={final['pairs']} stop={final['stop']}; correctness="
                 f"{'PASS' if final['gates'].get('correctness') else 'FAIL'}; "
                 f"mechanism={winner.get('mechanism')}; full={result_path}\n")
        (run_dir / "campaign-brief.txt").write_text(brief)
        print(json.dumps({"status": "complete", "run_id": plan["run_id"],
                          "phase": phase, "winner": final["winner"],
                          "improvement": final["baseline_vs_winner"]["point"],
                          "pairs": final["pairs"], "stop": final["stop"],
                          "result": str(result_path), "brief": str(run_dir / "campaign-brief.txt")},
                         separators=(",", ":")))
        return 0
    except Exception as exc:
        result = {"protocol": "profileweaver.campaign-result/v1", "status": "failed",
                  "run_id": plan["run_id"], "fingerprint": fingerprint,
                  "elapsed_s": time.perf_counter() - started, "error": str(exc)}
        write(result_path, result)
        print(json.dumps({"status": "failed", "run_id": plan["run_id"],
                          "error": str(exc), "result": str(result_path)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
