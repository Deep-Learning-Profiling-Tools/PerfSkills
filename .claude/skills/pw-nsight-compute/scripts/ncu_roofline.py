#!/usr/bin/env python3
"""Reduce NVIDIA's SpeedOfLight roofline counters into one bounded model."""

from __future__ import annotations

from ncu_csv import find_metric


PRECISIONS = {
    "fp64": {
        "peak": "derived__sm__sass_thread_inst_executed_op_dfma_pred_on_x2",
        "work": ["smsp__sass_thread_inst_executed_op_dadd_pred_on.sum.per_cycle_elapsed",
                 "smsp__sass_thread_inst_executed_op_dmul_pred_on.sum.per_cycle_elapsed",
                 "derived__smsp__sass_thread_inst_executed_op_dfma_pred_on_x2"],
    },
    "fp32": {
        "peak": "derived__sm__sass_thread_inst_executed_op_ffma_pred_on_x2",
        "work": ["smsp__sass_thread_inst_executed_op_fadd_pred_on.sum.per_cycle_elapsed",
                 "smsp__sass_thread_inst_executed_op_fmul_pred_on.sum.per_cycle_elapsed",
                 "derived__smsp__sass_thread_inst_executed_op_ffma_pred_on_x2"],
    },
}


def _value(metrics: dict, name: str):
    item = find_metric(metrics, name)
    return item.get("value") if item else None


def _compact(value):
    return float(f"{value:.7g}") if isinstance(value, (int, float)) else value


def reduce_roofline(metrics: dict) -> dict:
    """Match the equations in NVIDIA's SpeedOfLight_RooflineChart section."""
    sm_clock = _value(metrics, "sm__cycles_elapsed.avg.per_second")
    smsp_clock = _value(metrics, "smsp__cycles_elapsed.avg.per_second")
    dram_peak_cycle = _value(metrics, "dram__bytes.sum.peak_sustained")
    dram_clock = _value(metrics, "dram__cycles_elapsed.avg.per_second")
    achieved_bytes_s = _value(metrics, "dram__bytes.sum.per_second")
    peak_bytes_s = (dram_peak_cycle * dram_clock
                    if dram_peak_cycle is not None and dram_clock is not None else None)
    common_missing = []
    if sm_clock is None or smsp_clock is None:
        common_missing.append("sm-clock-rate")
    if peak_bytes_s is None:
        common_missing.append("dram-peak-rate")
    if achieved_bytes_s is None or achieved_bytes_s <= 0:
        common_missing.append("dram-achieved-rate")

    candidates = []
    for precision, spec in PRECISIONS.items():
        peak_cycle = _value(metrics, spec["peak"])
        work_cycle = [_value(metrics, name) for name in spec["work"]]
        if peak_cycle is None or any(value is None for value in work_cycle):
            continue
        achieved_flops_s = sum(work_cycle) * smsp_clock if smsp_clock else None
        peak_flops_s = peak_cycle * sm_clock if sm_clock else None
        if not achieved_flops_s or not peak_flops_s or not achieved_bytes_s or not peak_bytes_s:
            continue
        intensity = achieved_flops_s / achieved_bytes_s
        ridge = peak_flops_s / peak_bytes_s
        attainable = min(peak_flops_s, peak_bytes_s * intensity)
        speedup = attainable / achieved_flops_s if achieved_flops_s else None
        candidates.append({
            "precision": precision, "classification": (
                "memory-bound" if intensity < ridge else
                "compute-bound" if intensity > ridge else "balanced"),
            "arithmetic_intensity_flop_per_byte": _compact(intensity),
            "ridge_point_flop_per_byte": _compact(ridge),
            "achieved_gflops": _compact(achieved_flops_s / 1e9),
            "compute_peak_gflops": _compact(peak_flops_s / 1e9),
            "achieved_dram_gbs": _compact(achieved_bytes_s / 1e9),
            "dram_peak_gbs": _compact(peak_bytes_s / 1e9),
            "attainable_gflops": _compact(attainable / 1e9),
            "roofline_efficiency_pct": _compact(achieved_flops_s / attainable * 100),
            "kernel_speedup_ceiling": _compact(max(1.0, speedup)),
        })
    if not candidates:
        return {"status": "unavailable", "method": "ncu-speed-of-light-counters",
                "missing": common_missing or ["positive-fp32-or-fp64-work-rate"],
                "policy": "leave the Roofline and downstream Amdahl estimate null"}
    selected = max(candidates, key=lambda item: item["achieved_gflops"])
    return {"status": "complete", "method": "ncu-speed-of-light-counters",
            "confidence": "high", **selected,
            "measured_precisions": [{"precision": item["precision"],
                                     "achieved_gflops": item["achieved_gflops"]}
                                    for item in candidates]}
