#!/usr/bin/env python3
"""Parse a GPA gpa.advice file into structured, deduped, ranked JSON."""

import argparse
import json
import re
import sys

# Trailing value is the kernel's time, and its formatting varies between GPA builds: some
# emit an integer, others a float ("...: 0.000401347"). Matching only one form silently
# yields zero kernel blocks -- reported as "no findings" on an otherwise valid gpa.advice --
# so accept both. Group 2 is captured but never read (kernel timing comes from nsys, in
# pgo_bench.py), so widening it changes nothing downstream.
KERNEL_RE = re.compile(r"^GPU Kernel (.+): ([\d.]+(?:[eE][-+]?\d+)?)\s*$")
SECTION_NAMES = {"Code Optimizers", "Parallel Optimizers", "Binary Optimizers"}
APPLY_RE = re.compile(
    r"^\s*Apply (\w+) optimization, ratio ([\d.]+)%,\s*estimate speedup (inf|[\d.]+)x\s*$"
)
FROM_TO_RE = re.compile(r"^\s*(?:From|To) .* at (.+):(\d+)\s*$")
ADDR_RE = re.compile(r"^\s*0x[0-9a-fA-F]+ at Line (\d+)(?: in Loop at Line (\d+))?\s*$")
DASHES_RE = re.compile(r"^-{5,}\s*$")
STARS_RE = re.compile(r"^\*{5,}\s*$")
NUMBERED_BLAME_RE = re.compile(r"^\s*\d+\.\s*Hot BLAME")
NESTED_BLAME_RE = re.compile(r"^\s*Hot BLAME")
# Captures both the ratio AND the stall-reason token (e.g. "GINS:LAT_MTHR") -- the token was
# previously matched by \S+ but discarded, even though it's real data GPA prints (the specific
# hardware stall category behind this blame line) and not derivable any other way. Added
# 2026-08-09 so the cross-benchmark prior can be keyed on (optimizer, stall reason) instead of
# optimizer alone -- see build_global_index.py / get_optimizer_prior.py / references/design-history.md Phase 4.
BLAME_RATIO_RE = re.compile(r"Hot BLAME (\S+) code, ratio ([\d.]+)%")
ADJUST_RE = re.compile(r"^\s*Adjust ")

# Heuristic for "this finding's shape looks algorithmic, not instruction-level" (added
# 2026-08-09). Motivated by XSBench's material/energy lookup kernel, where a human
# investigation found 93%+ of blame concentrated on an energy-indexed global-memory gather --
# a data-layout problem (sort lookups before running them) that GPA's instruction-level
# PC-sampling profiler has no mechanism to propose a fix for; it took a human noticing the
# concentration to recognize the pattern. GINS:LAT_GMEM*/LAT_TMEM* are GPA's global/texture
# memory latency stall categories -- the ones a "your access pattern is scattered/indexed, not
# your code" story fits; GINS:LAT_IDEP_*/LAT_SYNC/etc. are instruction-dependency or
# synchronization stalls a code-level optimizer can plausibly still fix, so they're excluded.
#
# Verified against XSBench's real gpa.advice (2026-08-09): a single finding's locations are
# NOT one dominant address -- GPA's own duplicate-line quirk (each Hot BLAME/address pair is
# printed twice) plus genuinely many nearby addresses in the same gather loop means blame is
# spread across dozens of small-ratio locations. The concentration that matters is what
# FRACTION of a finding's own blamed locations (regardless of how many there are) share a
# memory-latency stall reason -- for XSBench's GPUCodeReorderOptimizer finding this comes out to
# ~99.7% (156.938 of 157.346 summed location-ratio units), matching the "93%+" figure once
# combined with the entry's own 93.732% kernel-time ratio.
MEMORY_STALL_PREFIXES = ("GINS:LAT_GMEM", "GINS:LAT_TMEM")
BLAME_CONCENTRATION_THRESHOLD = 90.0  # percent of a finding's summed location-ratio units
MIN_ENTRY_RATIO_FOR_ALGORITHMIC_FLAG = 50.0  # only flag findings that are themselves a real
                                              # contributor to kernel time, not noise-level ones


def is_structural(line: str) -> bool:
    return bool(
        FROM_TO_RE.match(line)
        or ADDR_RE.match(line)
        or DASHES_RE.match(line)
        or STARS_RE.match(line)
        or APPLY_RE.match(line)
        or KERNEL_RE.match(line)
        or NUMBERED_BLAME_RE.match(line)
        or NESTED_BLAME_RE.match(line)
        or ADJUST_RE.match(line)
        or line.strip() in SECTION_NAMES
    )


def parse_kernel_blocks(text: str):
    """Return a list of kernel blocks: {signature, entries: [...]}."""
    lines = text.splitlines()
    blocks = []
    cur_block = None
    cur_section = None
    cur_entry = None
    pending_file = None
    cur_blame_ratio = None  # ratio of the innermost "Hot BLAME ... ratio X%" line
                              # currently in scope -- governs the location(s) that
                              # follow, until the next Hot BLAME line updates it
    cur_blame_reason = None  # paired stall-reason token (e.g. "GINS:LAT_MTHR") for the same line

    def flush_entry():
        nonlocal cur_entry
        if cur_entry is not None and cur_block is not None:
            cur_entry["description"] = " ".join(cur_entry["description_lines"]).strip()
            del cur_entry["description_lines"]
            # highest-contributing location first -- the parser previously sorted
            # by (file, line), which does NOT correlate with which location is
            # actually responsible for the most stall cycles within this finding
            cur_entry["locations"] = sorted(
                cur_entry["locations"],
                key=lambda loc: (-(loc.get("ratio") or 0.0), loc["file"], loc["line"]),
            )
            cur_block["entries"].append(cur_entry)
        cur_entry = None

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        m = KERNEL_RE.match(line)
        if m:
            flush_entry()
            cur_section = None
            cur_block = {"signature": m.group(1), "entries": []}
            blocks.append(cur_block)
            continue

        if line.strip() in SECTION_NAMES:
            flush_entry()
            cur_section = line.strip()
            continue

        m = APPLY_RE.match(line)
        if m and cur_block is not None:
            flush_entry()
            speedup_raw = m.group(3)
            cur_entry = {
                "optimizer": m.group(1),
                "category": cur_section,
                "ratio": float(m.group(2)),
                "speedup": None if speedup_raw == "inf" else float(speedup_raw),
                "description_lines": [],
                "locations": [],
            }
            pending_file = None
            cur_blame_ratio = None
            cur_blame_reason = None
            continue

        if cur_entry is None:
            continue

        m = BLAME_RATIO_RE.search(line)
        if m:
            cur_blame_reason = m.group(1)
            cur_blame_ratio = float(m.group(2))
            continue

        m = FROM_TO_RE.match(line)
        if m:
            pending_file = m.group(1)
            continue

        m = ADDR_RE.match(line)
        if m and pending_file is not None:
            loc = {"file": pending_file, "line": int(m.group(1))}
            if m.group(2):
                loc["loopLine"] = int(m.group(2))
            if cur_blame_ratio is not None:
                loc["ratio"] = cur_blame_ratio
            if cur_blame_reason is not None:
                loc["stallReason"] = cur_blame_reason
            cur_entry["locations"].append(loc)
            continue

        if is_structural(line):
            continue

        stripped = line.strip()
        if stripped:
            cur_entry["description_lines"].append(stripped)

    flush_entry()
    return blocks


def dedupe_blocks(blocks):
    """Collapse kernel blocks that are exact repeats (same signature + same
    optimizer/ratio/speedup set) into one, tracking occurrence count."""
    deduped = []
    fingerprints = {}

    for block in blocks:
        fp_entries = tuple(
            sorted((e["optimizer"], e["ratio"], e["speedup"]) for e in block["entries"])
        )
        fp = (block["signature"], fp_entries)

        if fp in fingerprints:
            existing = fingerprints[fp]
            existing["occurrences"] += 1
            for new_e, existing_e in zip(block["entries"], existing["entries"]):
                seen = {(loc["file"], loc["line"]) for loc in existing_e["locations"]}
                for loc in new_e["locations"]:
                    if (loc["file"], loc["line"]) not in seen:
                        existing_e["locations"].append(loc)
            continue

        deduped_block = {
            "signature": block["signature"],
            "occurrences": 1,
            "entries": block["entries"],
        }
        fingerprints[fp] = deduped_block
        deduped.append(deduped_block)

    return deduped


def possibly_algorithmic(entry_ratio: float, locations: list[dict]) -> tuple[bool, str | None]:
    """(flag, note). True when >= BLAME_CONCENTRATION_THRESHOLD% of a finding's OWN blamed
    locations (summed by ratio, not just counted) share a memory-latency stall reason (see
    MEMORY_STALL_PREFIXES above), and the finding itself is a real contributor to kernel time
    (>= MIN_ENTRY_RATIO_FOR_ALGORITHMIC_FLAG%) -- a strong tell for "sort/restructure your
    data," a class of fix no instruction-level optimizer in this catalog can propose. A cheap,
    mechanical proxy for a pattern a human previously had to notice by hand; false negatives (a
    real algorithmic problem that doesn't happen to concentrate this cleanly) are expected and
    fine -- this flags the easy/obvious cases, it doesn't claim to catch every one.
    """
    if not locations or entry_ratio < MIN_ENTRY_RATIO_FOR_ALGORITHMIC_FLAG:
        return False, None
    total = sum(loc["ratio"] for loc in locations if loc.get("ratio") is not None)
    if total <= 0:
        return False, None
    mem_total = sum(
        loc["ratio"] for loc in locations
        if loc.get("ratio") is not None and (loc.get("stallReason") or "").startswith(MEMORY_STALL_PREFIXES)
    )
    mem_fraction_pct = 100.0 * mem_total / total
    if mem_fraction_pct >= BLAME_CONCENTRATION_THRESHOLD:
        note = (
            f"{mem_fraction_pct:.1f}% of this finding's blamed locations are memory-latency "
            f"stalls (GINS:LAT_GMEM*/LAT_TMEM*), and the finding itself accounts for "
            f"{entry_ratio:.1f}% of kernel time -- consider whether the access pattern itself "
            f"(indexing order, sort-before-lookup, gather/scatter restructuring) is the real "
            f"problem before trying instruction-level optimizers here; this shape is a known "
            f"case GPA's PC-sampling profiler can't propose a fix for on its own (see XSBench "
            f"precedent, design-history.md Phase 4 bonus)."
        )
        return True, note
    return False, None


def build_findings(deduped_blocks):
    findings = []
    informational = []

    for block in deduped_blocks:
        for entry in block["entries"]:
            if entry["ratio"] <= 0.0:
                continue

            # locations are already sorted highest-ratio-first (see flush_entry()) -- the
            # dominant stall reason is whichever location contributes the most blame, not
            # necessarily the first Hot BLAME line printed for this entry.
            dominant_stall_reason = (
                entry["locations"][0].get("stallReason") if entry["locations"] else None
            )
            algorithmic_flag, algorithmic_note = possibly_algorithmic(entry["ratio"], entry["locations"])

            record = {
                "kernel": block["signature"],
                "occurrences": block["occurrences"],
                "category": entry["category"],
                "optimizer": entry["optimizer"],
                "ratio": entry["ratio"],
                "speedup": entry["speedup"],
                "description": entry["description"],
                "locations": entry["locations"],
                "dominantStallReason": dominant_stall_reason,
                "possiblyAlgorithmic": algorithmic_flag,
                "possiblyAlgorithmicNote": algorithmic_note,
            }

            if entry["speedup"] is None:
                informational.append(record)
            else:
                record["impactScore"] = round(
                    (entry["ratio"] / 100.0) * max(entry["speedup"] - 1.0, 0.0), 6
                )
                findings.append(record)

    findings.sort(key=lambda r: r["impactScore"], reverse=True)
    informational.sort(key=lambda r: r["ratio"], reverse=True)
    return findings, informational


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("advice_path", help="Path to a gpa.advice file")
    parser.add_argument(
        "--top", type=int, default=None, help="Limit output to the top N findings by impact score"
    )
    args = parser.parse_args()

    with open(args.advice_path, "r") as f:
        text = f.read()

    blocks = parse_kernel_blocks(text)
    deduped_blocks = dedupe_blocks(blocks)
    findings, informational = build_findings(deduped_blocks)

    if args.top is not None:
        findings = findings[: args.top]

    result = {
        "sourceFile": args.advice_path,
        "kernelBlockCount": len(deduped_blocks),
        "findings": findings,
        "informational": informational,
    }

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
