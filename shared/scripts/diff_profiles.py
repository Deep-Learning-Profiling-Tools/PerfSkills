#!/usr/bin/env python3
"""Build an impact-ranked, source-aware delta from two normalized profiles.

Only metrics measured in both profiles are subtracted. Missing operations are reported as
unmatched rather than silently treated as zero, because normalized hotspot lists may be
truncated. Source-line deltas are emitted only when both observations carry file/line data.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def compact(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    return 0 if value == 0 else float(f"{value:.7g}")


def metric_values(item):
    values = {"self_time_s": item.get("self_time_s"), "pct_of_wall": item.get("pct_of_wall"), "calls": item.get("calls")}
    for name, value in (item.get("metrics") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[name] = value
    return {name: value for name, value in values.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}


def operation_key(item):
    if item.get("file") and item.get("line"):
        return f"line:{item['file']}:{item['line']}:{item.get('name', '')}"
    name = re.sub(r"\s+", " ", item.get("name", "")).strip()
    return "name:" + name


def display_entity(item):
    return {"name": item.get("name"), "kind": item.get("kind"), "file": item.get("file"), "line": item.get("line")}


def change_record(metric, before_item, after_item, before, after, base_wall):
    delta = after - before
    relative = None if before == 0 else delta / abs(before) * 100
    record = {**display_entity(after_item), "base": compact(before), "opt": compact(after),
              "delta": compact(delta), "change_pct": compact(relative)}
    if metric == "self_time_s" and base_wall:
        record["wall_impact_pct"] = compact(delta / base_wall * 100)
    return record


def pct_change(after, before):
    return compact((after / before - 1) * 100) if isinstance(before, (int, float)) and before and isinstance(after, (int, float)) else None


def command_args(command):
    if not isinstance(command, str):
        return None
    # Executable paths normally differ between baseline and variant; arguments define the workload.
    try:
        return shlex.split(command)[1:]
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--variant", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-targets", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-items", type=int, default=3)
    args = parser.parse_args()
    baseline, variant = load(args.baseline), load(args.variant)
    if not baseline or not variant:
        raise SystemExit("both normalized profiles are required")
    report, sources = load(args.report, {}) if args.report else {}, load(args.source_targets, {}) if args.source_targets else {}
    before_by_key = {operation_key(item): item for item in baseline.get("hotspots", [])}
    after_by_key = {operation_key(item): item for item in variant.get("hotspots", [])}
    common = sorted(before_by_key.keys() & after_by_key.keys())
    all_metrics = sorted({name for key in common for name in metric_values(before_by_key[key]).keys() & metric_values(after_by_key[key]).keys()})
    base_wall = (baseline.get("totals") or {}).get("wall_time_s")
    metric_deltas, line_deltas = [], []
    for metric in all_metrics:
        changes, line_changes = [], []
        for key in common:
            before_item, after_item = before_by_key[key], after_by_key[key]
            before_values, after_values = metric_values(before_item), metric_values(after_item)
            if metric not in before_values or metric not in after_values:
                continue
            record = change_record(metric, before_item, after_item, before_values[metric], after_values[metric], base_wall)
            if record["delta"] == 0:
                continue
            changes.append(record)
            if after_item.get("file") and after_item.get("line"):
                line_changes.append(record)
        decreases = sorted((x for x in changes if x["delta"] < 0), key=lambda x: x["delta"])
        increases = sorted((x for x in changes if x["delta"] > 0), key=lambda x: x["delta"], reverse=True)
        if changes:
            metric_deltas.append({"metric": metric, "decreases": decreases[:args.max_items],
                                  "increases": increases[:args.max_items], "matched_entities": len(changes)})
        if line_changes:
            line_decreases = sorted((x for x in line_changes if x["delta"] < 0), key=lambda x: x["delta"])
            line_increases = sorted((x for x in line_changes if x["delta"] > 0), key=lambda x: x["delta"], reverse=True)
            line_deltas.append({"metric": metric, "decreases": line_decreases[:args.max_items],
                                "increases": line_increases[:args.max_items]})

    bt, vt = baseline.get("totals") or {}, variant.get("totals") or {}
    workload_fields = ("run_command", "input", "ranks", "threads_per_rank", "gpus", "nodes")
    workload_comparison = {key: (baseline.get("workload") or {}).get(key) == (variant.get("workload") or {}).get(key) for key in workload_fields if key != "run_command"}
    workload_comparison["run_command_args"] = command_args((baseline.get("workload") or {}).get("run_command")) == command_args((variant.get("workload") or {}).get("run_command"))
    after_hot = variant.get("hotspots") or []
    residual = after_hot[0] if after_hot else None
    leverage = None
    if residual:
        pct = residual.get("pct_of_wall")
        leverage = {**display_entity(residual), "pct_of_variant_wall": compact(pct),
                    "amdahl_ceiling_if_serial_and_removable": compact(1 / (1 - pct / 100)) if isinstance(pct, (int, float)) and 0 <= pct < 100 else None}

    source_index = []
    for target in sources.get("targets", []):
        source = target.get("source") or {}
        source_index.append({"hotspot": target.get("hotspot"), "path": source.get("path"),
                             "lines": [source.get("start_line"), source.get("end_line")],
                             "slice": (target.get("slice") or {}).get("path"),
                             "confidence": (target.get("resolution") or {}).get("confidence")})
    target = report.get("reprofile") or {}
    capsules = [{
        "claim": "end-to-end profile movement",
        "observations": {"wall_change_pct": pct_change(vt.get("wall_time_s"), bt.get("wall_time_s")),
                         "gpu_busy_change_pct": pct_change(vt.get("gpu_busy_s"), bt.get("gpu_busy_s"))},
        "confidence": "single-profile-pair",
        "limit": "Use the benchmark confidence interval for significance; this pair establishes mechanism placement.",
    }]
    if target:
        capsules.append({"claim": "declared target movement", "observations": {
            "target_pct_before": target.get("target_pct_before"), "target_pct_after": target.get("target_pct_after"),
            "target_shrank": target.get("target_shrank")}, "source_ranges": source_index[:2],
            "confidence": "adjudicated-report", "limit": "Target grouping follows the report's declared normalization rule."})

    record = {
        "protocol": "profileweaver.profile-diff/v1", "baseline": str(args.baseline), "variant": str(args.variant),
        "comparability": {"same_tool": (baseline.get("tool") or {}).get("name") == (variant.get("tool") or {}).get("name"),
                          "same_tool_version": (baseline.get("tool") or {}).get("version") == (variant.get("tool") or {}).get("version"),
                          "workload_fields_equal": workload_comparison,
                          "all_declared_workload_fields_equal": all(workload_comparison.values())},
        "headline": {"wall_s": {"base": compact(bt.get("wall_time_s")), "opt": compact(vt.get("wall_time_s")),
                                  "change_pct": pct_change(vt.get("wall_time_s"), bt.get("wall_time_s"))},
                     "gpu_busy_s": {"base": compact(bt.get("gpu_busy_s")), "opt": compact(vt.get("gpu_busy_s")),
                                    "change_pct": pct_change(vt.get("gpu_busy_s"), bt.get("gpu_busy_s"))}},
        "metric_deltas": metric_deltas, "top_line_deltas": line_deltas,
        "unmatched": {"baseline_only": [display_entity(before_by_key[key]) for key in sorted(before_by_key.keys() - after_by_key.keys())[:args.max_items]],
                      "variant_only": [display_entity(after_by_key[key]) for key in sorted(after_by_key.keys() - before_by_key.keys())[:args.max_items]],
                      "policy": "Unmatched entries are not assigned a zero value; names may change and hotspot lists may be truncated."},
        "remaining_leverage": leverage, "decision_capsules": capsules,
        "context_selection": {"algorithm": "rank signed matched deltas by magnitude; cap each direction; emit lines only with measured file/line attribution",
                              "max_items_per_direction": args.max_items, "matched_entities": len(common),
                              "baseline_entities": len(before_by_key), "variant_entities": len(after_by_key),
                              "line_attribution_available": bool(line_deltas)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"status": "ok", "output": str(args.output), "matched": len(common),
                      "metrics": len(metric_deltas), "line_metrics": len(line_deltas),
                      "wall_change_pct": record["headline"]["wall_s"]["change_pct"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
