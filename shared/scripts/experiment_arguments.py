"""Arguments for the public experiment controller."""

import argparse
from pathlib import Path


def parse():
    parser = argparse.ArgumentParser(description="Measure, inspect and select correct optimization candidates.")
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
    for setup in (prepare_parser, batch_parser):
        setup.add_argument("--because", action="append", help="cite a delivered fact ID")
        setup.add_argument("--consider", action="append", help="next capability=rewrite hypothesis")
    usage = sub.add_parser("usage", help="record campaign-specific cumulative client token usage")
    usage.add_argument("--session-tokens", required=True, type=int)
    for setup in (prepare_parser, batch_parser):
        setup.add_argument("--target-kernel", help="new kernel selector after a rename or fusion")
    objective = sub.add_parser("objective", help="bind or revise the metric used to choose the leader")
    objective.add_argument("--scope", required=True, choices=["application", "kernel"])
    objective.add_argument("--kernel", help="regex selecting all kernels in the primary operation")
    objective.add_argument("--provider", help="consistent timing provider for kernel comparisons")
    objective.add_argument("--comparison-provider", help="reference profiler for a baseline/winner kernel comparison")
    objective.add_argument("--candidate", help="revise this candidate's renamed kernel selector")
    check_parser = sub.add_parser("check", help="adaptive build/validation checkpoint without paired timing")
    check_parser.add_argument("id")
    check_parser.add_argument("--build-only", action="store_true",
                              help="inspect compiled resources without executing; correctness stays unmeasured")
    check_parser.add_argument("--observe", action="store_true",
                              help="collect CUPTI activity and correctness in one validation execution; no ranking")
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("ids", nargs="+")
    evaluate_parser.add_argument("--ncu", action="append", default=[], metavar="ID",
                                 help="force a fresh full-set replay; automatic NCU/reuse is the default")
    evaluate_parser.add_argument("--evidence", choices=["auto", "none", "timeline", "kernel"], default="auto",
                                 help="adaptive policy evidence request; auto uses measured triggers")
    evaluate_parser.add_argument("--need", action="append", default=[],
                                 help="capability needed for the next edit, e.g. gpu-source-line; may repeat")
    evaluate_parser.add_argument("--provider", help="request a registered provider for the declared --need capability")
    evidence_parser = sub.add_parser("evidence", help="profile a measured snapshot without repeating paired benchmarks")
    evidence_parser.add_argument("id", nargs="?", default="current")
    evidence_parser.add_argument("--need", action="append", default=[])
    evidence_parser.add_argument("--evidence", choices=["timeline", "kernel"],
                                 help="broad profile-only question; --need requests specific capabilities")
    evidence_parser.add_argument("--provider")
    evidence_parser.add_argument("--recipe", action="store_true",
                                 help="reuse validated typed profiler parameters; rebind and reroute on environment drift")
    confirm_parser = sub.add_parser("confirm")
    confirm_parser.add_argument("ids", nargs="+")
    confirm_parser.add_argument("--ncu", action="append", default=[], metavar="ID",
                                help="force a fresh candidate replay before final confirmation")
    confirm_parser.add_argument("--fresh", action="store_true",
                                help="explicitly rerun final benchmarks and full winner profiles")
    confirm_parser.add_argument("--retry-evidence", action="store_true",
                                help="reuse unchanged passing final samples and retry only failed profiling/reporting")
    sub.add_parser("status")
    sub.add_parser("report", help="read the complete saved ledger; no measurements or token inventory")
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--section", required=True,
                                choices=["hotspots", "warnings", "benchmarks", "gates",
                                         "source", "harness", "artifacts", "routing", "ncu", "evidence", "costs", "decision", "fact"])
    inspect_parser.add_argument("--limit", type=int, default=20)
    inspect_parser.add_argument("--candidate")
    inspect_parser.add_argument("--group")
    inspect_parser.add_argument("--known", help="previous source content ID; return only changes")
    inspect_parser.add_argument("--target", help="one emitted source target id; returns at most 80 lines")
    inspect_parser.add_argument("--offset", type=int, default=0, help="source index or slice page offset")
    return parser.parse_args()
