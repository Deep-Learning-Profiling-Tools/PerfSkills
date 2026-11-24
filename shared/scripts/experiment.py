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
from adaptive_policy import enabled


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
    from experiment_arguments import parse
    args = parse()
    from contextlib import ExitStack
    from resource_meter import measure, token_usage
    meter = ExitStack()
    try:
        plan, run, variables = read_plan(args.plan)
        if args.command not in {"usage", "inspect", "status"}:
            meter.enter_context(measure(plan, run, "command", args.command))
        if args.command == "usage":
            result = token_usage(run, args.session_tokens)
        elif args.command == "objective":
            from objective_acquisition import select
            state, baseline = ensure_initialized(plan, run, variables)
            result = select(plan, run, state, scope=args.scope, pattern=args.kernel,
                            provider=args.provider, candidate=args.candidate, comparison_provider=args.comparison_provider)
        elif args.command == "report":
            from campaign_report import existing
            result = existing(plan, run)
        elif args.command == "init":
            state, _ = ensure_initialized(plan, run, variables)
            result = {"status": "initialized", "run_id": plan["run_id"],
                      "contract": state["contract_sha256"][:12],
                      **({"ncu": terminal_ncu((state.get("kernel_profiles") or {}).get("baseline"))}
                         if (state.get("kernel_profiles") or {}).get("baseline") else {})}
        elif args.command == "prepare":
            result = prepare(plan, run, variables, args)
        elif args.command == "prepare-batch":
            result = prepare_batch(plan, run, variables, args.candidate, because=args.because, consider=args.consider, target_kernel=args.target_kernel)
        elif args.command == "evaluate":
            state, baseline = ensure_initialized(plan, run, variables)
            if (args.evidence != "auto" or args.need or args.provider) and not enabled(plan):
                raise ValueError("--evidence requires the adaptive policy; legacy --ncu is unchanged")
            if args.provider and not args.need:
                raise ValueError("--provider requires an explicit --need capability")
            if args.ncu and (args.need or args.provider or args.evidence != "auto"):
                raise ValueError("--ncu cannot be combined with --need, --provider, or --evidence")
            result = run_campaign(plan, args.plan.resolve(), run, state, baseline, args.ids,
                                  "screen", diagnose_ids=args.ncu, evidence=args.evidence, needs=args.need or None,
                                  provider=args.provider)
        elif args.command == "evidence":
            from evidence_acquisition import acquire
            if args.recipe:
                if args.need or args.provider or args.evidence:
                    raise ValueError("--recipe cannot be combined with --need, --provider or --evidence")
                from skill_runtime import reuse_recipe
                parameters = reuse_recipe(plan, run)
                args.need, args.provider = parameters["needs"], parameters["provider"]
            if not args.need and not args.evidence:
                raise ValueError("evidence requires --need CAPABILITY or --evidence timeline|kernel")
            if args.provider and not args.need:
                raise ValueError("--provider requires an explicit --need capability")
            state, baseline = ensure_initialized(plan, run, variables)
            capsule = acquire(plan, run, state, baseline, args.id, args.need or None, args.provider,
                              surface=args.evidence or "kernel")
            result = {"status": capsule["status"], "new_benchmark_runs": 0,
                      "evidence": capsule.get("agent_packet") or capsule.get("acquisition")}
        elif args.command == "check":
            from candidate_check import check
            from acquisition_cost import event
            import time
            state, baseline = ensure_initialized(plan, run, variables)
            started = time.perf_counter()
            try:
                result = check(plan, run, state, baseline, args.id, args.build_only, args.observe)
            finally:
                event(run, "candidate-check", time.perf_counter() - started, candidate=args.id)
        elif args.command == "confirm":
            state, baseline = ensure_initialized(plan, run, variables)
            if args.ncu and not args.fresh:
                raise ValueError("--ncu requires --fresh; default confirmation reuses evidence")
            if args.retry_evidence and args.fresh:
                raise ValueError("--retry-evidence and --fresh are mutually exclusive")
            result = (run_campaign(plan, args.plan.resolve(), run, state, baseline, args.ids,
                                   "confirm", diagnose_ids=args.ncu, retry_evidence=args.retry_evidence)
                      if args.fresh or args.retry_evidence or (enabled(plan) and not state.get("confirmed"))
                      else promote_existing(plan, run, state, args.ids))
        elif args.command == "inspect":
            if args.offset < 0:
                raise ValueError("offset must be nonnegative")
            if args.section in {"decision", "fact"}:
                from rewrite_evidence import inspect_decision
                result = inspect_decision(run, args.section, args.candidate, args.target)
            elif args.section == "source" and args.group == "phase":
                from phase_source import inspect as inspect_phase
                result = inspect_phase(run, plan, args.candidate)
            elif args.section == "harness" and args.group == "link":
                from link_inspect import inspect as inspect_link
                result = inspect_link(run, plan, args.candidate, args.target, args.limit)
            elif args.section == "source":
                from source_inspect import inspect as source_inspect
                result = source_inspect(run, args.candidate, args.target, args.offset, args.limit, args.known)
            else:
                result = inspect(run, args.section, args.limit, args.candidate, args.group, args.offset)
        else:
            result = status(plan, run, variables)
            from selection_objective import notice
            from experiment_support import load
            result.update(notice(load(run / "experiment-state.json", {})))
        if result.get("campaign_complete") and (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3" and args.command in {"evaluate", "confirm"}:
            from campaign_report import compact
            from experiment_support import load
            result = compact(plan, run, load(run / "experiment-state.json", {}),
                             load(run / "measurement-verdict.json", {}) or result)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
        return 1
    finally:
        meter.close()


if __name__ == "__main__":
    raise SystemExit(main())
