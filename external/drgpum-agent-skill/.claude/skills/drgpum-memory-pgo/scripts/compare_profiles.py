#!/usr/bin/env python3
"""Compare two DrGPUM profiles captured before and after one source change.

    python scripts/compare_profiles.py <before_dir> <after_dir>

Both must use identical workload configuration -- same batch size, same input
data, same iteration count. A comparison across different configurations
measures the configuration, not the optimization.
"""
import sys
from pathlib import Path

# parents[4] is the repo root (holds agent/): scripts -> drgpum-memory-pgo ->
# skills -> .claude -> root. This was parents[3] when the skills lived at skills/;
# moving them under .claude/ added a level the offset did not follow.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agent.drgpum import human_bytes, parse_profile


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    before, after = parse_profile(argv[0]), parse_profile(argv[1])
    b, a = before.current_memory_peak, after.current_memory_peak
    print(f"  peak before : {human_bytes(b) if b else 'unknown'}")
    print(f"  peak after  : {human_bytes(a) if a else 'unknown'}")
    if b and a:
        pct = (b - a) / b * 100
        print(f"  reduction   : {human_bytes(b - a)}  ({pct:.1f}%)")
        if pct < 2:
            print("\n  Below the 2% noise floor -- termination clause 1 applies.")
    print("\n  This number counts only if correctness was verified separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
