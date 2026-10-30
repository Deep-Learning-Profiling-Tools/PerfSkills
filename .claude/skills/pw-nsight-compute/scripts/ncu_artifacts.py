#!/usr/bin/env python3
"""Write bounded NCU drilldowns and read rules/source samples."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from ncu_csv import compact_non_null, metric_priority, number, write


GROUP_PATTERNS = {
    "launch": (r"^launch__",),
    "occupancy": (r"occupancy", r"maximum_warps", r"warps_active"),
    "scheduler": (r"warps_(?:active|eligible|issued)", r"issue_(?:active|inst0)"),
    "stalls": (r"stalled_",),
    "instructions": (r"(?:^|__)inst(?:_|ructions)", r"sass_", r"opcode"),
    "control_flow": (r"branch", r"diverg", r"thread_inst_executed"),
    "memory": (r"^(?:l1tex|lts|dram|gpu__dram|mmu|fbp|memory)__", r"(?:sector|request|transaction|bank_conflict|local|global|shared|cache|hit_rate|bytes)"),
    "throughput": (r"throughput", r"pct_of_peak_sustained", r"cycles_active"),
    "roofline": (r"flop", r"fma", r"tensor", r"arithmetic", r"compute_memory_throughput", r"dram_throughput"),
}


def write_drilldowns(grouped_rows: dict[str, list[dict]], directory: Path,
                     max_metrics: int) -> dict:
    """Write bounded semantic groups; raw CSV remains the lossless fallback."""
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for group, patterns in GROUP_PATTERNS.items():
        by_kernel = []
        matched_total = selected_total = 0
        for kernel, rows in grouped_rows.items():
            matches = [item for item in rows if item.get("value") is not None and
                       any(re.search(pattern, item["metric"]) for pattern in patterns)]
            unique = {}
            for item in sorted(matches, key=metric_priority):
                unique.setdefault(item["metric"], item)
            selected = list(unique.values())[:max_metrics]
            matched_total += len(unique)
            selected_total += len(selected)
            if selected:
                by_kernel.append({"name": kernel, "metrics": [
                    {"raw_metric": item["metric"], "value": item["value"], "unit": item.get("unit")}
                    for item in selected]})
        if not by_kernel:
            continue
        path = directory / f"{group}.json"
        write(path, {"protocol": "profileweaver.ncu-drilldown/v1", "group": group,
                     "kernels": by_kernel, "context": {"matched": matched_total,
                     "selected": selected_total, "omitted": matched_total - selected_total,
                     "max_metrics_per_kernel": max_metrics}})
        result[group] = {"path": str(path), "matched": matched_total,
                         "selected": selected_total, "omitted": matched_total - selected_total}
    return result


def read_rules(path: Path | None, maximum: int = 5) -> list[dict]:
    if not path or not path.is_file():
        return []
    rules = {}
    with path.open(newline="", errors="replace") as handle:
        for row in csv.DictReader(handle):
            name = row.get("Rule Name") or ""
            description = row.get("Rule Description") or ""
            if not name and not description:
                continue
            speedup = number(row.get("Estimated Speedup"))
            key_ = name or description[:80]
            item = {"name": name or None, "type": row.get("Rule Type") or None,
                    "description": description[:240] or None,
                    "estimated_speedup_type": row.get("Estimated Speedup Type") or None,
                    "estimated_speedup": speedup}
            previous = rules.get(key_)
            if previous is None or (speedup or -1) > (previous.get("estimated_speedup") or -1):
                rules[key_] = item
    return sorted(rules.values(), key=lambda item: (item.get("estimated_speedup") is None,
                  -(item.get("estimated_speedup") or 0), item.get("name") or ""))[:maximum]


def read_source_hotspots(path: Path | None, maximum: int = 5) -> list[dict]:
    if not path or not path.is_file():
        return []
    rows = list(csv.reader(path.open(newline="", errors="replace")))
    header_at = next((i for i, row in enumerate(rows) if row[:2] == ["Address", "Source"]), None)
    if header_at is None:
        return []
    header = rows[header_at]
    records = []
    for values in rows[header_at + 1:]:
        if len(values) != len(header):
            continue
        row = dict(zip(header, values))
        samples = number(row.get("# Samples")) or 0
        not_issued = number(row.get("Warp Stall Sampling (Not-issued Samples)")) or 0
        if not samples and not not_issued:
            continue
        stalls = []
        for label, raw in row.items():
            if label.startswith("stall_") and label.endswith("(Not Issued)"):
                value = number(raw)
                if value:
                    stalls.append((label.removesuffix(" (Not Issued)").removeprefix("stall_"), value))
        stalls.sort(key=lambda item: item[1], reverse=True)
        records.append(compact_non_null({
            "address": row.get("Address"), "instruction": (row.get("Source") or "").strip()[:120],
            "samples": samples, "not_issued_samples": not_issued,
            "dominant_not_issued_stall": ({"reason": stalls[0][0], "samples": stalls[0][1]}
                                           if stalls else None),
            "avg_threads_executed": number(row.get("Avg. Threads Executed")),
            "divergent_branches": number(row.get("Divergent Branches")),
            "l1_shared_conflicts": number(row.get("L1 Conflicts Shared N-Way")),
            "l2_excessive_sectors": number(row.get("L2 Theoretical Sectors Global Excessive")),
        }))
    records.sort(key=lambda item: (item.get("not_issued_samples", 0), item.get("samples", 0)),
                 reverse=True)
    return records[:maximum]
