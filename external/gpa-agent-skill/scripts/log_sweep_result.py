#!/usr/bin/env python3
"""Append one line to state/sweep-log.jsonl -- the audit trail for a multi-
benchmark sweep (see SKILL.md's "Sweep mode" section). One compact line per
benchmark per sweep, so reviewing what a sweep did is a `tail`, not a re-read
of every benchmark's pgo-ledger.md.

This is a pure append -- it never rewrites earlier lines -- unlike
global-optimizer-index.json, which is a fully-rebuilt-from-scratch aggregate.
pgo-ledger.md per benchmark remains the source of truth for KEPT/REVERTED
decisions; this log only mirrors the outcome for cross-benchmark review.

Usage:
  log_sweep_result.py <benchmark> --status exhausted
  log_sweep_result.py <benchmark> --status attempted --kernel K --optimizer O \\
      --decision KEPT [--e2e-speedup 1.23]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
LOG_PATH = SKILL_DIR / "state" / "sweep-log.jsonl"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("benchmark")
    parser.add_argument("--status", required=True, choices=["attempted", "exhausted"])
    parser.add_argument("--kernel")
    parser.add_argument("--optimizer")
    parser.add_argument("--decision", choices=["KEPT", "REVERTED"])
    parser.add_argument("--e2e-speedup", type=float, dest="e2e_speedup")
    args = parser.parse_args()

    if args.status == "attempted" and not (args.kernel and args.optimizer and args.decision):
        sys.exit("--status attempted requires --kernel, --optimizer, and --decision")

    entry = {"ts": datetime.now(timezone.utc).isoformat(), "benchmark": args.benchmark, "status": args.status}
    if args.status == "attempted":
        entry["kernel"] = args.kernel
        entry["optimizer"] = args.optimizer
        entry["decision"] = args.decision
        if args.e2e_speedup is not None:
            entry["e2eSpeedup"] = args.e2e_speedup

    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"Appended to {LOG_PATH}: {entry}", file=sys.stderr)


if __name__ == "__main__":
    main()
