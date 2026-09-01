#!/usr/bin/env python3
"""Filtered cross-benchmark prior for a benchmark's current top candidate findings.

Stage D of the PGO-skill memory pipeline: rather than handing the agent the
full global-optimizer-index.json (every optimizer, every benchmark), this joins
it against just the optimizers named by THIS benchmark's current top findings
(profile-summary.json) and returns one small annotation per candidate -- "has
this optimizer worked on any OTHER benchmark, on a bottleneck that looks like
this one?" This benchmark's own history is deliberately excluded here; that's
what pgo-ledger-index.json / SKILL.md step 3 already covers, so nothing is
shown twice.

Returns TWO priors per candidate (added 2026-08-09, Phase 4), not one:
- sameStallReason: history where BOTH the optimizer class AND GPA's own
  dominant stall-reason code (finding["dominantStallReason"], e.g.
  "GINS:LAT_IDEP_SMEM") match this candidate -- the actually-predictive
  signal, since two findings sharing an optimizer class but not a stall
  reason may have nothing in common mechanistically.
- anyStallReason: history for the optimizer class regardless of stall
  reason -- the old (pre-Phase-4) behavior, kept alongside rather than
  dropped, since most historical ledger entries predate stall-reason
  tracking and land under an "(unknown stall reason)" bucket that would
  otherwise make sameStallReason look emptier than the real signal is.

Depends on profile-summary.json (run get_profile_summary.py first) and
state/global-optimizer-index.json (run build_global_index.py first) already
being present -- this script only reads and filters, it does not regenerate
either upstream stage.

Usage: get_optimizer_prior.py <benchmark>
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

SKILL_DIR = SCRIPT_DIR.parent
from _gpa_root import resolve_gpa_root, resolve_toolchain_root, require_benchmark_dir  # noqa: E402
GPA_ROOT = resolve_gpa_root(SKILL_DIR)  # $PW_GPA_ROOT, else legacy parents[2]
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"
GLOBAL_INDEX_PATH = SKILL_DIR / "state" / "global-optimizer-index.json"


def load_benchmark_dir(name: str) -> Path:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    if name not in config:
        sys.exit(f"Unknown benchmark '{name}'. Known: {', '.join(config)}")
    path = GPA_ROOT / config[name]["dir"]
    require_benchmark_dir(path, name, GPA_ROOT)
    return path


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: get_optimizer_prior.py <benchmark>")
    name = sys.argv[1]
    bench_dir = load_benchmark_dir(name)

    summary_path = bench_dir / "gpa-database" / "profile-summary.json"
    if not summary_path.exists():
        sys.exit(f"No {summary_path} -- run get_profile_summary.py first")
    summary = json.loads(summary_path.read_text())

    if not GLOBAL_INDEX_PATH.exists():
        sys.exit(f"No {GLOBAL_INDEX_PATH} -- run build_global_index.py first")
    global_index = json.loads(GLOBAL_INDEX_PATH.read_text())["index"]

    def summarize(buckets: list[dict]) -> dict:
        """elsewhere-filtered kept/reverted counts + per-benchmark detail, pooled across
        however many (optimizer, stallReason) buckets are passed in."""
        kept, reverted, benchmarks = 0, 0, {}
        for bucket in buckets:
            for bname, kernels in bucket["benchmarks"].items():
                if bname == name:
                    continue
                benchmarks.setdefault(bname, {})
                for kernel, sizes in kernels.items():
                    benchmarks[bname].setdefault(kernel, {}).update(sizes)
                    for v in sizes.values():
                        if v["decision"] == "KEPT":
                            kept += 1
                        else:
                            reverted += 1
        return {"keptCount": kept, "revertedCount": reverted, "benchmarks": benchmarks}

    candidates = []
    for finding in summary["findings"]:
        optimizer = finding["optimizer"]
        stall_reason = finding.get("dominantStallReason") or "(unknown stall reason)"

        same_bucket = global_index.get(f"{optimizer}|{stall_reason}")
        # anyStallReason pools every bucket for this optimizer regardless of stall reason --
        # composite keys are "<optimizer>|<stallReason>", so match on the optimizer prefix.
        any_buckets = [
            b for k, b in global_index.items() if b.get("optimizer") == optimizer
        ]

        candidates.append(
            {
                "kernel": finding["kernel"],
                "optimizer": optimizer,
                "dominantStallReason": finding.get("dominantStallReason"),
                "ratio": finding["ratio"],
                "impactScore": finding.get("impactScore"),
                "priorElsewhere": {
                    "sameStallReason": summarize([same_bucket] if same_bucket else []),
                    "anyStallReason": summarize(any_buckets),
                },
            }
        )

    print(json.dumps({"benchmark": name, "candidates": candidates}, indent=2))


if __name__ == "__main__":
    main()
