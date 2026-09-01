"""
Command-line entry point.

    python -m agent.drgpum <drgpum_output_dir> [--json out.json] [--top N]
                           [--min-bytes N] [--min-idle-kernels N]

`<drgpum_output_dir>` is the directory written by:

    gvprof -v -e memory_liveness ./your_app
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .detectors import DEFAULT_MIN_BYTES, DEFAULT_MIN_IDLE_KERNELS, analyze
from .parser import DrGPUMParseError, parse_profile
from .report import format_report, to_json


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agent.drgpum",
        description="Find memory inefficiencies in a DrGPUM memory_liveness profile.",
    )
    p.add_argument("profile_dir", help="directory written by `gvprof -e memory_liveness`")
    p.add_argument("--json", metavar="PATH", help="also write findings as JSON")
    p.add_argument("--top", type=int, default=10, help="findings to print (default 10)")
    p.add_argument(
        "--tensors",
        action="store_true",
        help="analyse PyTorch sub-allocations (tensors) instead of allocator "
             "segments; requires a torch-monitor build",
    )
    p.add_argument(
        "--min-bytes",
        type=int,
        default=DEFAULT_MIN_BYTES,
        help=f"ignore objects smaller than this (default {DEFAULT_MIN_BYTES})",
    )
    p.add_argument(
        "--min-idle-kernels",
        type=int,
        default=DEFAULT_MIN_IDLE_KERNELS,
        help=f"ignore idle spans shorter than this (default {DEFAULT_MIN_IDLE_KERNELS})",
    )
    args = p.parse_args(argv)

    try:
        profile = parse_profile(args.profile_dir)
    except DrGPUMParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.tensors:
        if not profile.has_submemory:
            print(
                "error: no sub-allocation data in this profile. Rebuild the "
                "profiler with torch-monitor and run gvprof with "
                "-ck HPCRUN_SANITIZER_TORCH_ANALYSIS=1",
                file=sys.stderr,
            )
            return 2
        profile = profile.submemory_view()

    findings = analyze(
        profile,
        min_bytes=args.min_bytes,
        min_idle_kernels=args.min_idle_kernels,
    )

    print(format_report(profile, findings, top=args.top))

    if args.json:
        Path(args.json).write_text(to_json(profile, findings))
        print(f"\nJSON written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
