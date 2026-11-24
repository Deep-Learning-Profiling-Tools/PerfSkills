"""Publish baseline facts promptly and bound adaptive profiler acquisition."""

from datetime import datetime, timezone
import json
import math
import sys

from experiment_support import load, write


def adaptive(plan):
    return (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3"


def progress(plan, run, stage, status, **facts):
    if not adaptive(plan):
        return
    row = {"event": "baseline-progress", "run_id": run.name, "stage": stage,
           "status": status, "utc": datetime.now(timezone.utc).isoformat(), **facts}
    previous = load(run / "baseline-status.json", {})
    write(run / "baseline-status.json", previous | row)
    path = run / "raw/experiment/baseline-progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    # Keep stdout reserved for the final machine response; stderr is live progress.
    print(json.dumps(row, separators=(",", ":")), file=sys.stderr, flush=True)


def capture_timeout(plan, run):
    explicit = (plan.get("profiling") or {}).get("baseline_capture_timeout_s")
    wall = (load(run / "baseline-timing.json", {}).get("sample") or {}).get("wall_s")
    budget = explicit if explicit is not None else max(30, 3 * wall) if isinstance(wall, (int, float)) else 30
    if not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        raise ValueError("baseline_capture_timeout_s must be a positive finite number")
    return min(budget, (plan.get("execution") or {}).get("timeout_s") or budget)
