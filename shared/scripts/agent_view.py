#!/usr/bin/env python3
"""Write one bounded ProfileWeaver decision packet or an explicit drill-down."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_view_build import DEFAULT_MAX_BYTES, build_view, drilldown
# These aliases preserve the original public facade used by harness_support.py.
from agent_view_decision import select_variant_profile as select_variant_profile
from agent_view_profile import load as load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--section", choices=["hotspots", "warnings", "benchmarks", "gates",
                                               "source", "harness", "artifacts"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--print-packet", action="store_true")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    if args.section:
        print(json.dumps(drilldown(run, args.section, args.limit), indent=2, ensure_ascii=False))
        return 0
    packet, path = build_view(run, args.max_bytes, args.output.resolve() if args.output else None)
    if args.print_packet:
        print(json.dumps(packet, separators=(",", ":"), ensure_ascii=False))
    else:
        print(json.dumps({"run_id": packet["run_id"], "state": packet["state"],
                          "action": packet["decision"]["action"], "packet": str(path),
                          "bytes": packet["context"]["packet_bytes"],
                          "estimated_tokens": packet["context"]["estimated_tokens"],
                          "evidence_reduction_ratio": packet["context"]["reduction_ratio"]},
                         separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
