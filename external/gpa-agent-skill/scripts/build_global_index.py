#!/usr/bin/env python3
"""Aggregate every benchmark's ledger data into one cross-benchmark view, keyed
by (optimizer, dominant stall reason) instead of by benchmark.

Where build_ledger_index.py answers "has this kernel+optimizer been tried on
THIS benchmark?", this answers "has this optimizer worked on ANY benchmark,
on a bottleneck that looks like this one?" -- the cross-benchmark narrowing
step: what were 10 separate per-benchmark tables collapse into one table.

Keyed on (optimizer, stallReason) rather than optimizer alone (changed
2026-08-09, Phase 4) because optimizer-only was a popularity count, not a
predictive signal: "GPUWarpBalanceOptimizer: 1 KEPT/0 REVERTED" doesn't say
WHY it worked, so it can't distinguish a genuinely similar bottleneck from a
coincidentally-same optimizer class applied to an unrelated stall pattern.
stallReason is GPA's own "GINS:LAT_*" vocabulary (e.g. "GINS:LAT_IDEP_SMEM"),
scanned from each ledger entry's prose via build_ledger_index.py -- entries
that never mention it land under UNKNOWN_STALL_REASON rather than being
dropped. Keys are encoded as "<optimizer>|<stallReason>" strings (JSON object
keys must be strings); optimizer/stallReason are also stored as separate
fields in each bucket so callers don't need to re-split the key.

Every <benchmark>/gpa-database/pgo-ledger.md remains the sole source of truth;
this file is always fully rebuilt from those 10 files (never hand-edited), and
the write is skipped when the combined content hash is unchanged.

Usage: build_global_index.py
"""

import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import build_ledger_index  # noqa: E402

SKILL_DIR = SCRIPT_DIR.parent
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"
STATE_DIR = SKILL_DIR / "state"
INDEX_PATH = STATE_DIR / "global-optimizer-index.json"


def build() -> dict:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    source_hashes: dict[str, str | None] = {}
    aggregate: dict[str, dict] = {}

    for name in sorted(config):
        bench_dir, kernel_vocab = build_ledger_index.load_benchmark(name)
        ledger_path = bench_dir / "gpa-database" / "pgo-ledger.md"
        if not ledger_path.exists():
            source_hashes[name] = None
            continue

        ledger_text = ledger_path.read_text()
        source_hashes[name] = hashlib.sha256(ledger_text.encode()).hexdigest()
        per_benchmark_index = build_ledger_index.build_index(ledger_text, kernel_vocab)

        for kernel, optimizers in per_benchmark_index.items():
            for optimizer, sizes in optimizers.items():
                for problem_size, info in sizes.items():
                    stall_reason = info.get("stallReason", build_ledger_index.UNKNOWN_STALL_REASON)
                    composite_key = f"{optimizer}|{stall_reason}"
                    bucket = aggregate.setdefault(
                        composite_key,
                        {
                            "optimizer": optimizer,
                            "stallReason": stall_reason,
                            "keptCount": 0,
                            "revertedCount": 0,
                            "benchmarks": {},
                        },
                    )
                    bucket["benchmarks"].setdefault(name, {}).setdefault(kernel, {})[problem_size] = {
                        "decision": info["decision"],
                        "lastUpdated": info["lastUpdated"],
                    }
                    if info["decision"] == "KEPT":
                        bucket["keptCount"] += 1
                    else:
                        bucket["revertedCount"] += 1

    combined_hash = hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
    return {"sourceHashes": source_hashes, "combinedHash": combined_hash, "index": aggregate}


def main():
    result = build()

    if INDEX_PATH.exists():
        try:
            existing = json.loads(INDEX_PATH.read_text())
        except json.JSONDecodeError:
            existing = None
        if existing and existing.get("combinedHash") == result["combinedHash"]:
            print(json.dumps(existing, indent=2))
            print(f"(up to date -- {INDEX_PATH} already reflects all known ledgers)", file=sys.stderr)
            return

    STATE_DIR.mkdir(exist_ok=True)
    INDEX_PATH.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    pair_count = sum(len(v["benchmarks"]) for v in result["index"].values())
    print(
        f"Wrote {INDEX_PATH} ({len(result['index'])} (optimizer, stallReason) buckets, "
        f"{pair_count} benchmark entries)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
