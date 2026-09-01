#!/usr/bin/env python3
"""Parse a DrGPUM memory_liveness directory and rank its inefficiencies.

    python scripts/analyze_profile.py <profile_dir> [--json out.json] [--top N]

Deterministic: every number printed is computed from the profiler's own output.
Run this before forming any hypothesis about the workload.
"""
import sys
from pathlib import Path

# The skill is self-contained at the doc level but shares one implementation
# with the project, so there is a single source of truth for the formats.
# parents[4] is the repo root (holds agent/): scripts -> drgpum-memory-pgo ->
# skills -> .claude -> root. This was parents[3] when the skills lived at skills/;
# moving them under .claude/ added a level the offset did not follow.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agent.drgpum.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
