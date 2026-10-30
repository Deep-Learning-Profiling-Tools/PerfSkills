#!/usr/bin/env python3
"""Reduce one NCU kernel and classify its measured bottleneck."""

from __future__ import annotations

import re

from ncu_csv import compact_non_null, contains_metric, field, find_metric
from ncu_roofline import reduce_roofline


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
            evidence.append(field(item))
            seen.add(item["metric"])
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
        "roofline": reduce_roofline(metrics),
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
    memory = kernel.get("memory") or {}
    load_bytes = val(memory.get("global_load_bytes_per_sector"))
    if (load_bytes is not None and load_bytes < 16):
        cls = "memory-transaction-waste"
    elif ((is_percent and value >= 30) or (not is_percent and value >= 10)) and (
            "scoreboard" in reason or "mio" in reason):
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
    if load_bytes is not None and load_bytes < 16:
        evidence.append({"metric": "global_load_bytes_per_sector", "value": load_bytes,
                         "threshold": "less than 16 useful bytes per 32-byte sector",
                         "supports": "global load transactions use less than half each sector"})
    if top:
        evidence.append({"metric": top["raw_metric"], "value": top["value"],
                         "threshold": ("30% for a dominant stall classification" if is_percent else None),
                         "supports": f"largest measured warp stall is {reason}"})
    if not evidence:
        evidence.append({"metric": "ncu.metric_count", "value": kernel.get("metric_count", 0),
                         "threshold": None, "supports": "counter capture completed; no decisive threshold matched"})
    return {"class": cls, "scope": kernel.get("name"), "evidence": evidence,
            "confidence": "medium" if cls != "indeterminate" else "low", "alternatives": []}
