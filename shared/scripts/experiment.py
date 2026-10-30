#!/usr/bin/env python3
"""Public CLI for ProfileWeaver's measured optimization loop.

``start`` creates a run from a workload and performs baseline initialization in
``experiment_start.py``.  Every later command consumes the generated plan:
prepare a candidate workspace, evaluate it, inspect evidence, confirm a winner,
or report status.  Supporting modules are backend implementation details; an
agent only invokes this file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from candidate_workspace import prepare, prepare_batch
from experiment_campaign import run_campaign
from experiment_finalize import promote_existing
from experiment_inspect import inspect
from experiment_state import ensure_initialized, read_plan, status
# Keep the historical import surface used by smoke tests and external callers.
from kernel_evidence import compact_ncu as compact_ncu
from terminal_view import terminal_ncu


def main() -> int:
    # Start has no plan yet. All other commands receive the plan it creates.
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        try:
            from experiment_start import run
            print(json.dumps(run(sys.argv[2:]), separators=(",", ":")))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--id", required=True)
    prepare_parser.add_argument("--parent", default="current",
                                help="baseline, current measured best, or a candidate id (default: current)")
    prepare_parser.add_argument("--mechanism", required=True)
    prepare_parser.add_argument("--description", required=True)
    batch_parser = sub.add_parser("prepare-batch")
    batch_parser.add_argument("--candidate", action="append", required=True,
                              help="exactly one ID:PARENT:MECHANISM[:DESCRIPTION] (PARENT=baseline, current, or candidate id); evaluate before the next candidate")
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("ids", nargs="+")
    evaluate_parser.add_argument("--ncu", action="append", default=[], metavar="ID",
                                 help="force a fresh full-set replay; automatic NCU/reuse is the default")
    confirm_parser = sub.add_parser("confirm")
    confirm_parser.add_argument("ids", nargs="+")
    confirm_parser.add_argument("--ncu", action="append", default=[], metavar="ID",
                                help="force a fresh candidate replay before final confirmation")
    confirm_parser.add_argument("--fresh", action="store_true",
                                help="explicitly rerun final benchmarks and full winner profiles")
    sub.add_parser("status")
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--section", required=True,
                                choices=["hotspots", "warnings", "benchmarks", "gates",
                                         "source", "harness", "artifacts", "routing", "ncu"])
    inspect_parser.add_argument("--limit", type=int, default=20)
    inspect_parser.add_argument("--candidate")
    inspect_parser.add_argument("--group")
    args = parser.parse_args()
    try:
        plan, run, variables = read_plan(args.plan)
        if args.command == "init":
            state, _ = ensure_initialized(plan, run, variables)
            result = {"status": "initialized", "run_id": plan["run_id"],
                      "contract": state["contract_sha256"][:12],
                      **({"ncu": terminal_ncu((state.get("kernel_profiles") or {}).get("baseline"))}
                         if (state.get("kernel_profiles") or {}).get("baseline") else {})}
        elif args.command == "prepare":
            result = prepare(plan, run, variables, args)
        elif args.command == "prepare-batch":
            result = prepare_batch(plan, run, variables, args.candidate)
        elif args.command == "evaluate":
            state, baseline = ensure_initialized(plan, run, variables)
            result = run_campaign(plan, args.plan.resolve(), run, state, baseline, args.ids,
                                  "screen", diagnose_ids=args.ncu)
        elif args.command == "confirm":
            state, baseline = ensure_initialized(plan, run, variables)
            if args.ncu and not args.fresh:
                raise ValueError("--ncu requires --fresh; default confirmation reuses evidence")
            result = (run_campaign(plan, args.plan.resolve(), run, state, baseline, args.ids,
                                   "confirm", diagnose_ids=args.ncu)
                      if args.fresh else promote_existing(plan, run, state, args.ids))
        elif args.command == "inspect":
            result = inspect(run, args.section, args.limit, args.candidate, args.group)
        else:
            result = status(plan, run, variables)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
