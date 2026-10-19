#!/usr/bin/env python3
"""Reduce Nsight Compute raw CSV into bounded diagnostics and a normalized profile."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shlex
import sys
from collections import defaultdict
from pathlib import Path


PROTOCOL = "profileweaver.ncu-diagnostics/v1"

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


def load(path: Path | None, default=None):
    if not path:
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def key(row: dict, wanted: str):
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    return normalized.get(re.sub(r"[^a-z0-9]", "", wanted.lower()))


def number(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "nan", "inf", "-inf"}:
        return None
    scale = 1.0
    suffixes = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}
    match = re.fullmatch(r"([-+0-9.eE]+)\s*([kKmMgGtT]?)", text)
    if not match:
        return None
    if match.group(2):
        scale = suffixes[match.group(2).lower()]
    try:
        value = float(match.group(1)) * scale
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def read_metrics(path: Path):
    lines = path.read_text(errors="replace").splitlines()
    # NCU sometimes prefixes diagnostics before its CSV header. The raw page is wide in current
    # releases (one column per metric); older/synthetic exports can be tall.
    start = next((i for i, line in enumerate(lines) if "Kernel Name" in line and
                  ("Metric Name" in line or "Process ID" in line)), None)
    if start is None:
        raise ValueError(f"no NCU raw metric header in {path}")
    parsed = list(csv.reader(lines[start:]))
    header = parsed[0]
    rows = []
    if "Metric Name" in header and "Metric Value" in header:
        for source in csv.DictReader(lines[start:]):
            metric = key(source, "Metric Name")
            if metric:
                rows.append({"kernel": key(source, "Kernel Name") or "unknown-kernel",
                             "metric": str(metric), "unit": key(source, "Metric Unit"),
                             "value": number(key(source, "Metric Value")),
                             "value_raw": key(source, "Metric Value")})
        return rows

    units = parsed[1] if len(parsed) > 1 else [""] * len(header)
    kernel_index = header.index("Kernel Name")
    identity = {"ID", "Process ID", "Process Name", "Host Name", "Kernel Name", "Context",
                "Stream", "Block Size", "Grid Size", "Device", "CC"}
    for record in parsed[2:]:
        if len(record) != len(header):
            continue
        kernel = record[kernel_index] or "unknown-kernel"
        for index, metric in enumerate(header):
            if metric in identity:
                continue
            value = number(record[index])
            if value is None:
                continue
            rows.append({"kernel": kernel, "metric": metric, "unit": units[index] or None,
                         "value": value, "value_raw": record[index]})
    return rows


def find_metric(metrics: dict, *names: str):
    for name in names:
        item = metrics.get(name)
        if item and item.get("value") is not None:
            return item
    return None


def contains_metric(metrics: dict, *parts: str):
    matches = [item for name, item in metrics.items()
               if all(part in name for part in parts) and item.get("value") is not None]
    return max(matches, key=lambda item: abs(item["value"])) if matches else None


def field(item):
    if not item:
        return None
    return {"value": item["value"], "unit": item.get("unit"), "raw_metric": item["metric"]}


def compact_non_null(value):
    if isinstance(value, dict):
        return {k: compact_non_null(v) for k, v in value.items() if v is not None}
    return value


def metric_priority(item: dict):
    name = item["metric"]
    score = 0
    if name.endswith((".pct", ".ratio", ".sum", ".avg")):
        score += 4
    if "pct_of_peak_sustained" in name or "per_cycle_active" in name:
        score += 4
    if not name.startswith("breakdown:"):
        score += 2
    if any(text in name for text in ("peak_sustained", "max_rate", "device__attribute")):
        score -= 2
    return (-score, name)


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
            matched_total += len(unique); selected_total += len(selected)
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


def reduce_kernel(name: str, rows: list[dict], max_stalls: int, max_evidence: int):
    metrics = {}
    for item in rows:
        metrics.setdefault(item["metric"], item)

    regs = find_metric(metrics, "launch__registers_per_thread")
    static_shared = find_metric(metrics, "launch__shared_mem_per_block_static")
    dynamic_shared = find_metric(metrics, "launch__shared_mem_per_block_dynamic")
    block = find_metric(metrics, "launch__block_size")
    grid = find_metric(metrics, "launch__grid_size")
    waves = find_metric(metrics, "launch__waves_per_multiprocessor")
    duration = find_metric(metrics, "gpu__time_duration.sum")
    achieved_occ = find_metric(metrics, "sm__warps_active.avg.pct_of_peak_sustained_active")
    theoretical_occ = find_metric(metrics, "sm__maximum_warps_per_active_cycle_pct")
    if not theoretical_occ:
        theoretical_occ = contains_metric(metrics, "occupancy", "theoretical")
    active_warps = find_metric(metrics, "smsp__warps_active.avg.per_cycle_active",
                               "smsp__warps_active.avg")
    eligible_warps = find_metric(metrics, "smsp__warps_eligible.avg.per_cycle_active",
                                 "smsp__warps_eligible.avg")
    issued_warps = find_metric(metrics, "smsp__issue_active.avg.per_cycle_active",
                               "smsp__warps_issued.avg")
    issue_active = find_metric(metrics, "smsp__issue_active.avg.pct_of_peak_sustained_active")
    sm_throughput = find_metric(metrics, "sm__throughput.avg.pct_of_peak_sustained_elapsed")
    compute_throughput = find_metric(metrics, "gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed")
    dram_throughput = find_metric(metrics, "gpu__dram_throughput.avg.pct_of_peak_sustained_elapsed",
                                  "dram__throughput.avg.pct_of_peak_sustained_elapsed")
    l1_throughput = find_metric(metrics, "l1tex__throughput.avg.pct_of_peak_sustained_active",
                                "l1tex__throughput.avg.pct_of_peak_sustained_elapsed")
    l2_throughput = find_metric(metrics, "lts__throughput.avg.pct_of_peak_sustained_elapsed")
    l1_hit = contains_metric(metrics, "l1tex__t_sector_hit_rate", "pct")
    l2_hit = contains_metric(metrics, "lts__t_sector_hit_rate", "pct")
    instructions = find_metric(metrics, "smsp__inst_executed.sum")
    branch_eff = contains_metric(metrics, "branch", "efficiency")
    branch_pct = find_metric(metrics, "derived__smsp__inst_executed_op_branch_pct")
    threads_per_inst = find_metric(metrics, "smsp__thread_inst_executed_per_inst_executed.ratio")
    sectors_per_request = contains_metric(metrics, "average", "sectors_per_request")
    global_load_bytes_per_sector = find_metric(
        metrics, "smsp__sass_average_data_bytes_per_sector_mem_global_op_ld.ratio")
    global_store_bytes_per_sector = find_metric(
        metrics, "smsp__sass_average_data_bytes_per_sector_mem_global_op_st.ratio")
    local_load_bytes_per_sector = find_metric(
        metrics, "smsp__sass_average_data_bytes_per_sector_mem_local_op_ld.ratio")
    dram_read_bytes = find_metric(metrics, "dram__bytes_read.sum")
    dram_write_bytes = find_metric(metrics, "dram__bytes_write.sum")
    spill_requests_pct = find_metric(metrics, "derived__local_spilling_requests_pct")
    shared_conflicts = find_metric(metrics, "l1tex__data_bank_conflicts_pipe_lsu_mem_shared.sum")
    theoretical_warps = find_metric(metrics, "sm__maximum_warps_avg_per_active_cycle")

    pipe_metrics = []
    for metric, item in metrics.items():
        match = re.fullmatch(r"sm__inst_executed_pipe_(.+?)\.avg\.pct_of_peak_sustained_active", metric)
        if match and item.get("value") is not None:
            pipe_metrics.append({"pipe": match.group(1), **field(item)})
    pipe_metrics.sort(key=lambda item: item["value"], reverse=True)

    stalls = []
    for metric, item in metrics.items():
        match = re.search(r"warp_issue_stalled_(.+?)_per_warp_active(?:\.pct)?$", metric)
        if not match:
            match = re.search(r"average_warps_issue_stalled_(.+?)_per_issue_active\.ratio$", metric)
        if match and item.get("value") is not None:
            stalls.append({"reason": match.group(1), "value": item["value"],
                           "unit": item.get("unit"), "raw_metric": metric})
    stalls.sort(key=lambda item: item["value"], reverse=True)
    stalls = stalls[:max_stalls]

    limiters = []
    for label, metric in (
        ("registers", "launch__occupancy_limit_registers"),
        ("shared_memory", "launch__occupancy_limit_shared_mem"),
        ("blocks", "launch__occupancy_limit_blocks"),
        ("warps", "launch__occupancy_limit_warps"),
    ):
        item = find_metric(metrics, metric)
        if item:
            limiters.append({"resource": label, **field(item)})

    signals = []
    def signal(kind, summary, evidence):
        signals.append({"kind": kind, "summary": summary, "evidence": evidence})

    if stalls:
        signal("dominant-warp-stall", f"{stalls[0]['reason']} is the largest comparable measured stall indicator",
               stalls[0])
    if eligible_warps and active_warps and eligible_warps["value"] < 1 <= active_warps["value"]:
        signal("scheduler-starvation", "fewer than one eligible warp per scheduler despite active warps",
               field(eligible_warps))
    if achieved_occ and theoretical_occ and theoretical_occ["value"] - achieved_occ["value"] >= 15:
        signal("occupancy-gap", "achieved occupancy is at least 15 percentage points below theoretical",
               {"achieved": field(achieved_occ), "theoretical": field(theoretical_occ)})
    if dram_throughput and dram_throughput["value"] >= 80:
        signal("dram-throughput-high", "DRAM throughput is at least 80% of sustained peak",
               field(dram_throughput))
    if sm_throughput and sm_throughput["value"] >= 80:
        signal("sm-throughput-high", "SM throughput is at least 80% of sustained peak",
               field(sm_throughput))
    if branch_eff and branch_eff["value"] < 80:
        signal("branch-efficiency-low", "measured branch efficiency is below 80%",
               field(branch_eff))

    evidence_order = [regs, duration, achieved_occ, theoretical_occ, active_warps,
                      eligible_warps, issued_warps, issue_active, sm_throughput,
                      compute_throughput, dram_throughput, l1_throughput, l2_throughput,
                      l1_hit, l2_hit, sectors_per_request, instructions]
    evidence = []
    seen = set()
    for item in evidence_order:
        if item and item["metric"] not in seen:
            evidence.append(field(item)); seen.add(item["metric"])
        if len(evidence) >= max_evidence:
            break

    return compact_non_null({
        "name": name,
        "duration": field(duration),
        "launch": {"grid_threads": field(grid), "block_threads": field(block),
                   "registers_per_thread": field(regs), "static_shared_bytes": field(static_shared),
                   "dynamic_shared_bytes": field(dynamic_shared), "waves_per_sm": field(waves)},
        "occupancy": {"achieved_pct": field(achieved_occ),
                      "theoretical": field(theoretical_occ),
                      "theoretical_active_warps_per_sm": field(theoretical_warps),
                      "limiters": limiters},
        "scheduler": {"active_warps_per_scheduler": field(active_warps),
                      "eligible_warps_per_scheduler": field(eligible_warps),
                      "issued_warps_per_scheduler": field(issued_warps),
                      "issue_active_pct": field(issue_active)},
        "warp_stalls": stalls,
        "throughput": {"sm_pct": field(sm_throughput), "compute_memory_pct": field(compute_throughput),
                       "dram_pct": field(dram_throughput), "l1_pct": field(l1_throughput),
                       "l2_pct": field(l2_throughput)},
        "memory": {"l1_hit_pct": field(l1_hit), "l2_hit_pct": field(l2_hit),
                   "sectors_per_request": field(sectors_per_request),
                   "global_load_bytes_per_sector": field(global_load_bytes_per_sector),
                   "global_store_bytes_per_sector": field(global_store_bytes_per_sector),
                   "local_load_bytes_per_sector": field(local_load_bytes_per_sector),
                   "dram_read_bytes": field(dram_read_bytes),
                   "dram_write_bytes": field(dram_write_bytes),
                   "local_spill_requests_pct": field(spill_requests_pct),
                   "shared_bank_conflicts": field(shared_conflicts)},
        "instructions": {"executed": field(instructions), "branch_efficiency_pct": field(branch_eff),
                         "branch_instruction_pct": field(branch_pct),
                         "threads_executed_per_instruction": field(threads_per_inst),
                         "top_compute_pipes": pipe_metrics[:4]},
        "signals": signals[:3], "evidence": evidence,
        "metric_count": len(metrics),
    })


def choose_bottleneck(kernel: dict):
    stalls = kernel.get("warp_stalls") or []
    top = stalls[0] if stalls else {}
    reason, value = top.get("reason", ""), top.get("value", 0)
    through = kernel.get("throughput") or {}
    def val(item): return (item or {}).get("value")
    unit = str(top.get("unit", "")).lower()
    is_percent = "%" in unit or "percent" in unit
    if is_percent and value >= 30 and ("scoreboard" in reason or "mio" in reason):
        cls = "memory-latency"
    elif is_percent and value >= 30 and ("barrier" in reason or "wait" in reason):
        cls = "sync"
    elif (val(through.get("dram_pct")) or 0) >= 80:
        cls = "memory-bandwidth"
    elif (val(through.get("sm_pct")) or 0) >= 80:
        cls = "compute-bound"
    else:
        cls = "indeterminate"
    evidence = []
    if top:
        evidence.append({"metric": top["raw_metric"], "value": top["value"],
                         "threshold": ("30% for a dominant stall classification" if is_percent else None),
                         "supports": f"largest measured warp stall is {reason}"})
    if not evidence:
        evidence.append({"metric": "ncu.metric_count", "value": kernel.get("metric_count", 0),
                         "threshold": None, "supports": "counter capture completed; no decisive threshold matched"})
    return {"class": cls, "scope": kernel.get("name"), "evidence": evidence,
            "confidence": "medium" if cls != "indeterminate" else "low", "alternatives": []}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--capture-metadata", type=Path, required=True)
    parser.add_argument("--details-csv", type=Path)
    parser.add_argument("--source-csv", type=Path)
    parser.add_argument("--timeline-profile", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=["baseline", "variant", "reprofile"], required=True)
    parser.add_argument("--variant")
    parser.add_argument("--run-command", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostics-output", type=Path, required=True)
    parser.add_argument("--max-kernels", type=int, default=2)
    parser.add_argument("--max-stalls", type=int, default=3)
    parser.add_argument("--max-evidence", type=int, default=12)
    parser.add_argument("--drilldown-dir", type=Path)
    parser.add_argument("--max-group-metrics", type=int, default=48)
    args = parser.parse_args()

    capture = load(args.capture_metadata, {})
    export_paths = {item.get("page"): Path(item["path"]) for item in capture.get("exports", [])
                    if item.get("returncode") == 0 and item.get("path")}
    details_path = args.details_csv or export_paths.get("details")
    source_path = args.source_csv or export_paths.get("source")
    rows = read_metrics(args.csv)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["kernel"]].append(row)
    drilldown_dir = args.drilldown_dir or args.diagnostics_output.parent / "ncu-drilldowns"
    drilldowns = write_drilldowns(grouped, drilldown_dir, args.max_group_metrics)
    kernels = [reduce_kernel(name, values, args.max_stalls, args.max_evidence)
               for name, values in grouped.items()]
    kernels.sort(key=lambda item: ((item.get("duration") or {}).get("value") or 0), reverse=True)
    omitted = max(0, len(kernels) - args.max_kernels)
    kernels = kernels[:args.max_kernels]
    diagnostics = {
        "protocol": PROTOCOL, "run_id": args.run_id,
        "collection": {"requested": capture.get("requested"), "resolved": capture.get("resolved"),
                       "timing": capture.get("timing"),
                       "report": (capture.get("capture") or {}).get("report")},
        "kernels": kernels,
        "rules": read_rules(details_path),
        "source_hotspots": read_source_hotspots(source_path),
        "drilldowns": drilldowns,
        "context": {"max_kernels": args.max_kernels, "max_stalls_per_kernel": args.max_stalls,
                    "max_evidence_per_kernel": args.max_evidence,
                    "omitted_kernels": omitted, "raw_metric_rows": len(rows),
                    "policy": "consume this capsule; load one named drilldown only when it changes the next action; raw report and full metrics stay under raw/"},
    }
    write(args.diagnostics_output, diagnostics)

    timeline = load(args.timeline_profile, {})
    top = kernels[0] if kernels else {"name": "unknown-kernel"}
    totals = timeline.get("totals") or {"wall_time_s": 0, "gpu_busy_s": None,
                                       "attributed_s": None}
    hotspots = timeline.get("hotspots") or [{
        "rank": 1, "name": top["name"], "kind": "gpu-kernel", "file": None, "line": None,
        "module": None, "self_time_s": 0, "total_time_s": None, "pct_of_wall": 0,
        "calls": None, "samples": None, "callpath": None, "metrics": {}, "amdahl_ceiling": 1,
    }]
    for hotspot in hotspots:
        if hotspot.get("name") == top.get("name") or len(hotspots) == 1:
            hotspot["metrics"] = (hotspot.get("metrics") or {}) | {"ncu": top}
            break
    capture_command = (capture.get("capture") or {}).get("command", "")
    run_command = args.run_command or (timeline.get("workload") or {}).get("run_command") or ""
    profile = {
        "run_id": args.run_id, "phase": args.phase, "variant": args.variant,
        "tool": {"name": "NVIDIA Nsight Compute",
                 "version": (capture.get("tool") or {}).get("version") or "unknown",
                 "mode": "kernel replay/full section set",
                 "command": capture_command, "sampling_hz": None,
                 "events": [item["metric"] for item in rows[:20]], "overhead_factor": None,
                 "multiplexed": None},
        "workload": (timeline.get("workload") or {"run_command": run_command}) | {"run_command": run_command},
        "machine": timeline.get("machine") or {}, "totals": totals, "hotspots": hotspots,
        "bottleneck": choose_bottleneck(top), "kernel_diagnostics": diagnostics,
        "warnings": (timeline.get("warnings") or []) + ([{
            "kind": "missing-timeline",
            "detail": "NCU replay is not an end-to-end timer; join this profile to Nsight Systems before ranking impact",
            "magnitude": None,
        }] if not timeline else []),
        "raw_artifacts": [str(args.csv), str(args.capture_metadata),
                          (capture.get("capture") or {}).get("report")],
    }
    profile["raw_artifacts"] = [item for item in profile["raw_artifacts"] if item]
    write(args.output, profile)
    elapsed = ((capture.get("timing") or {}).get("capture_elapsed_s"))
    print(json.dumps({"status": "complete", "kernels": len(kernels), "metrics": len(rows),
                      "ncu_capture_s": elapsed, "diagnostics": str(args.diagnostics_output),
                      "profile": str(args.output)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
        sys.exit(2)
