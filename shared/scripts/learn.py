#!/usr/bin/env python3
"""Maintain conservative cost/outcome priors from closed ProfileWeaver runs.

The ledger may tune broad routing costs and suggest repetition budgets after at least three
recorded runs for a child.
It never generates code changes, weakens trust gates, or treats a correlation as a cause.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DEFAULT_STORE = REPO / "runs/_knowledge/outcomes.jsonl"
DEFAULT_POLICY = REPO / "runs/_knowledge/policy.json"


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def read_ledger(path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def median(values):
    values = [float(x) for x in values if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return statistics.median(values) if values else None


def observation(run):
    report = load(run / "report.json")
    baseline = load(run / "profile.baseline.json", {})
    if not report:
        raise ValueError("report.json is required; learning only consumes adjudicated runs")
    workload = baseline.get("workload") or {}
    machine = baseline.get("machine") or {}
    cost = report.get("cost_actual") or load(run / "cost.json", {}) or {}
    measurement = report.get("measurement") or {}
    gates = report.get("gates") or {}
    failed = sorted(name for name, value in gates.items() if not (value or {}).get("passed"))
    normalized_workload = {k: workload.get(k) for k in ("run_command", "input", "ranks", "threads_per_rank", "gpus", "nodes")}
    workload_hash = hashlib.sha256(json.dumps(normalized_workload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    bottleneck = (report.get("diagnosis") or {}).get("bottleneck_class") or (baseline.get("bottleneck") or {}).get("class")
    return {
        "protocol": "profileweaver.outcome/v1",
        "run_id": report.get("run_id") or run.name,
        "ingested_utc": datetime.now(timezone.utc).isoformat(),
        "skill": report.get("skill"), "tool": report.get("tool"),
        "bottleneck": bottleneck, "workload_hash": workload_hash,
        "machine": {"cpu": machine.get("cpu_model"), "gpu": machine.get("gpu_model")},
        "status": report.get("status"), "winner": report.get("winner"),
        "speedup": measurement.get("speedup"), "speedup_ci95": measurement.get("speedup_ci95"),
        "benchmark_reps": measurement.get("reps"),
        "profiled_runs": cost.get("profiled_runs"), "benchmark_runs": cost.get("benchmark_runs"),
        "wall_clock_s": cost.get("wall_clock_s"), "overhead_factor": cost.get("observed_overhead_factor"),
        "target_shrank": (report.get("reprofile") or {}).get("target_shrank"),
        "failed_gates": failed,
    }


def aggregate(rows):
    groups = defaultdict(list)
    for row in rows:
        if row.get("skill"):
            groups[row["skill"]].append(row)
    skills = {}
    for skill, items in sorted(groups.items()):
        failed = Counter(gate for item in items for gate in item.get("failed_gates", []))
        verified = sum(item.get("status") == "verified" for item in items)
        skills[skill] = {
            "runs": len(items), "eligible": len(items) >= 3,
            "verified_rate": verified / len(items),
            "median_overhead_factor": median(x.get("overhead_factor") for x in items),
            "median_profiled_runs": median(x.get("profiled_runs") for x in items),
            "median_benchmark_runs": median(x.get("benchmark_runs") for x in items),
            "median_benchmark_reps": median(x.get("benchmark_reps") for x in items),
            "median_verified_speedup": median(x.get("speedup") for x in items if x.get("status") == "verified"),
            "common_failed_gates": [{"gate": name, "runs": count} for name, count in failed.most_common(3)],
            "allowed_uses": ["routing cost tie-break", "profiling and benchmark budget suggestion"] if len(items) >= 3 else [],
        }
    return {
        "protocol": "profileweaver.policy/v1", "generated_utc": datetime.now(timezone.utc).isoformat(),
        "minimum_recorded_runs": 3,
        "guardrail": "Priors may tune cost and budgets. They cannot skip correctness, significance, mechanism, or reprofile gates and cannot generate source rewrites.",
        "skills": skills,
    }


def write_all(store, policy_path, rows):
    store.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda x: x["run_id"])
    store_tmp = store.with_name(store.name + ".tmp")
    store_tmp.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    store_tmp.replace(store)
    policy = aggregate(rows)
    policy_tmp = policy_path.with_name(policy_path.name + ".tmp")
    policy_tmp.write_text(json.dumps(policy, indent=2) + "\n")
    policy_tmp.replace(policy_path)
    return policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("run_dir", type=Path)
    for command in (ingest, sub.add_parser("summary")):
        command.add_argument("--store", type=Path, default=DEFAULT_STORE)
        command.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args()
    rows = read_ledger(args.store)
    if args.command == "ingest":
        item = observation(args.run_dir.resolve())
        rows = [row for row in rows if row.get("run_id") != item["run_id"]] + [item]
        policy = write_all(args.store, args.policy, rows)
        learned = policy["skills"].get(item.get("skill"), {})
        print(json.dumps({"status": "ingested", "run_id": item["run_id"], "skill": item.get("skill"),
                          "recorded_runs": learned.get("runs"), "policy_eligible": learned.get("eligible"),
                          "store": str(args.store), "policy": str(args.policy)}, separators=(",", ":")))
        return 0
    policy = aggregate(rows)
    print(json.dumps({"runs": len(rows), "skills": policy["skills"], "guardrail": policy["guardrail"]},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
