#!/usr/bin/env python3
"""
ML-PGO: single entry point.

    python main.py profile -- python3 train.py    # run under DrGPUM
    python main.py analyze runs/baseline/...      # parse + rank findings
    python main.py agent   runs/baseline/...      # analyze + ask Claude
    python main.py compare runs/before runs/after # before/after peaks
    python main.py ledger                         # what has been tried

`profile` needs the DrGPUM toolchain on PATH and a GPU; everything else runs
anywhere, including on a laptop against a profile copied off the GPU machine.
See README.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.drgpum import DrGPUMParseError, analyze, format_report, parse_profile, to_json
from agent.ledger import Ledger


def cmd_profile(args) -> int:
    from agent.helpers.benchmark_runner import ProfilerNotFound, profile

    cmd = [c for c in args.command if c != "--"]
    if not cmd:
        print("error: no command given (put it after --)", file=sys.stderr)
        return 2
    try:
        out = profile(cmd, label=args.label, runs_dir=args.runs_dir)
    except (ProfilerNotFound, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"\nprofile written to: {out}")
    print(f"next: python main.py analyze {out}")
    return 0


def cmd_analyze(args) -> int:
    profile = parse_profile(args.profile_dir)
    findings = analyze(profile, min_bytes=args.min_bytes)
    print(format_report(profile, findings, top=args.top))
    if args.json:
        Path(args.json).write_text(to_json(profile, findings))
        print(f"\nJSON written to {args.json}")
    return 0


def cmd_agent(args) -> int:
    from agent.ml_pgo_agent import MLPGOAgent

    agent = MLPGOAgent(model=args.model)
    agent.run(args.profile_dir, optimized_dir=args.optimized, source=args.source)
    return 0


def cmd_compare(args) -> int:
    from agent.drgpum import human_bytes

    before, after = parse_profile(args.before), parse_profile(args.after)
    b, a = before.current_memory_peak, after.current_memory_peak
    print(f"  peak before : {human_bytes(b) if b else 'unknown'}")
    print(f"  peak after  : {human_bytes(a) if a else 'unknown'}")
    if b and a:
        print(f"  reduction   : {human_bytes(b - a)}  ({(b - a) / b * 100:.1f}%)")
        if (b - a) / b * 100 < 2:
            print("\n  Below the 2% noise floor -- see the termination clauses in .claude/skills/drgpum-memory-pgo/SKILL.md.")
    print("\n  A peak reduction only counts if correctness was verified separately.")
    return 0


def cmd_ledger(args) -> int:
    print(Ledger(args.path).format())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Find and fix GPU memory inefficiencies with DrGPUM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="run a workload under DrGPUM (needs a GPU)")
    sp.add_argument("--label", default="baseline")
    sp.add_argument("--runs-dir", default="runs")
    sp.add_argument("command", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_profile)

    sa = sub.add_parser("analyze", help="parse a profile and rank inefficiencies")
    sa.add_argument("profile_dir")
    sa.add_argument("--top", type=int, default=10)
    sa.add_argument("--min-bytes", type=int, default=64 * 1024)
    sa.add_argument("--json")
    sa.set_defaults(func=cmd_analyze)

    sg = sub.add_parser("agent", help="analyze, then ask Claude for source-level fixes")
    sg.add_argument("profile_dir")
    sg.add_argument("--optimized", help="second profile, enables validation")
    sg.add_argument("--source", help="application source, for locating findings")
    sg.add_argument("--model", default="claude-opus-5")
    sg.set_defaults(func=cmd_agent)

    sc = sub.add_parser("compare", help="before/after peak memory")
    sc.add_argument("before")
    sc.add_argument("after")
    sc.set_defaults(func=cmd_compare)

    sl = sub.add_parser("ledger", help="show what has been tried")
    sl.add_argument("path", nargs="?", default="runs/ledger.jsonl")
    sl.set_defaults(func=cmd_ledger)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DrGPUMParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
