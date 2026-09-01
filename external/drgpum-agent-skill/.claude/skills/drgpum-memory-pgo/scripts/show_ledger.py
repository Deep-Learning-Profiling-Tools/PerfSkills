#!/usr/bin/env python3
"""Show the append-only record of optimization attempts.

    python scripts/show_ledger.py [path/to/ledger.jsonl]

Consult this before proposing a fix: it is how the agent answers "what have we
already tried?" without re-reading the whole conversation.
"""
import sys
from pathlib import Path

# parents[4] is the repo root (holds agent/): scripts -> drgpum-memory-pgo ->
# skills -> .claude -> root. This was parents[3] when the skills lived at skills/;
# moving them under .claude/ added a level the offset did not follow.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from agent.ledger import Ledger

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "runs/ledger.jsonl"
    print(Ledger(path).format())
