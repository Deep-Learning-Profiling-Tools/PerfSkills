#!/usr/bin/env python3
"""Cached wrapper around parse_advice.py, keyed by gpa.advice's content hash.

Writes/reads <benchmark-dir>/gpa-database/profile-summary.json so a session
that calls this repeatedly for the same unchanged gpa.advice doesn't re-parse
1500-4000 lines of raw advice text every time. If the cache is missing,
corrupt, or stale (source hash or --top differs), it's rebuilt from
gpa.advice, which remains the single source of truth -- the cache is purely
an accelerant, never a hard dependency.

Usage: get_profile_summary.py <benchmark> [--top N] [--force]
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import parse_advice  # noqa: E402

SKILL_DIR = SCRIPT_DIR.parent
from _gpa_root import resolve_gpa_root, resolve_toolchain_root, require_benchmark_dir  # noqa: E402
GPA_ROOT = resolve_gpa_root(SKILL_DIR)  # $PW_GPA_ROOT, else legacy parents[2]
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"

DEFAULT_TOP = 5


def load_benchmark_dir(name: str) -> Path:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    if name not in config:
        sys.exit(f"Unknown benchmark '{name}'. Known: {', '.join(config)}")
    path = GPA_ROOT / config[name]["dir"]
    require_benchmark_dir(path, name, GPA_ROOT)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--force", action="store_true", help="Ignore cache, always re-parse")
    args = parser.parse_args()

    bench_dir = load_benchmark_dir(args.benchmark)
    advice_path = bench_dir / "gpa-database" / "gpa.advice"
    summary_path = bench_dir / "gpa-database" / "profile-summary.json"

    if not advice_path.exists():
        sys.exit(f"No gpa.advice at {advice_path} -- run run_gpa_profile.py first")

    advice_text = advice_path.read_text()
    source_hash = hashlib.sha256(advice_text.encode()).hexdigest()

    if not args.force and summary_path.exists():
        try:
            cached = json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            cached = None
        if cached and cached.get("sourceHash") == source_hash and cached.get("top") == args.top:
            print(json.dumps(cached, indent=2))
            print(f"(cache hit -- {summary_path} unchanged since last profile)", file=sys.stderr)
            return

    blocks = parse_advice.parse_kernel_blocks(advice_text)
    deduped_blocks = parse_advice.dedupe_blocks(blocks)
    findings, informational = parse_advice.build_findings(deduped_blocks)
    if args.top is not None:
        findings = findings[: args.top]

    result = {
        "sourceFile": str(advice_path),
        "sourceHash": source_hash,
        "top": args.top,
        "kernelBlockCount": len(deduped_blocks),
        "findings": findings,
        "informational": informational,
    }
    summary_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"(cache miss -- regenerated {summary_path})", file=sys.stderr)


if __name__ == "__main__":
    main()
