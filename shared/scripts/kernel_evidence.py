#!/usr/bin/env python3
"""Compact normalized NCU data without discarding optimization-critical evidence."""

from __future__ import annotations

from experiment_support import compact_number, nested
from kernel_advisor import advise_kernel


def _prune(value):
    if isinstance(value, dict):
        return {key: _prune(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_prune(item) for item in value if item is not None]
    return value


_AGENT_ADVICE_FIELDS = ("primary", "alternatives", "hierarchy", "models")


def agent_ncu(profile: dict | None) -> dict | None:
    """Keep measured NCU facts and GPA-style classifications for the agent.

    Candidate mechanisms, actions, priorities, and profiler rule advice remain backend
    state.  They must not become part of the compact agent packet.
    """
    if not profile:
        return profile
    result = {key: value for key, value in profile.items()
              if key not in {"advice", "rules"}}
    advice = profile.get("advice") or {}
    result["advice"] = {key: advice[key] for key in _AGENT_ADVICE_FIELDS
                         if key in advice}
    return result


def compact_ncu(profile: dict, trial_counts: dict[str, int] | None = None,
                observed_speedup: float | None = None,
                source_features: list[str] | None = None) -> dict | None:
    diagnostics = profile.get("kernel_diagnostics") or {}
    kernels = diagnostics.get("kernels") or []
    if not kernels:
        return None
    kernel = kernels[0]

    def value(section, name):
        return ((kernel.get(section) or {}).get(name) or {}).get("value")

    occupancy = kernel.get("occupancy") or {}
    timing = ((diagnostics.get("collection") or {}).get("timing") or {})
    requested = ((diagnostics.get("collection") or {}).get("requested") or {})
    source = ((diagnostics.get("source_hotspots") or [None])[0]) or {}
    matching = next((item for item in (profile.get("hotspots") or [])
                     if item.get("name") == kernel.get("name")), None)
    if matching is None and len(profile.get("hotspots") or []) == 1:
        matching = profile["hotspots"][0]
    pct = (matching or {}).get("pct_of_wall")
    hotspot_fraction = (pct / 100 if isinstance(pct, (int, float)) and 0 < pct <= 100
                        else None)
    capsule = _prune({
        "kernel": kernel.get("name"), "section": requested.get("section"),
        "duration_ns": ((kernel.get("duration") or {}).get("value")),
        "capture_s": timing.get("capture_elapsed_s"),
        "export_s": timing.get("export_elapsed_s"),
        "total_s": timing.get("total_elapsed_s"),
        "metric_count": (diagnostics.get("context") or {}).get("raw_metric_rows"),
        "launch": {"grid": value("launch", "grid_threads"),
                   "block": value("launch", "block_threads"),
                   "registers_per_thread": value("launch", "registers_per_thread"),
                   "static_shared_bytes": value("launch", "static_shared_bytes"),
                   "dynamic_shared_bytes": value("launch", "dynamic_shared_bytes"),
                   "waves_per_sm": value("launch", "waves_per_sm")},
        "occupancy": {"achieved_pct": value("occupancy", "achieved_pct"),
                      "theoretical_pct": value("occupancy", "theoretical"),
                      "limiters": [{"resource": item.get("resource"), "value": item.get("value")}
                                   for item in occupancy.get("limiters", [])]},
        "scheduler": {"active_warps": value("scheduler", "active_warps_per_scheduler"),
                      "eligible_warps": value("scheduler", "eligible_warps_per_scheduler"),
                      "issued_warps": value("scheduler", "issued_warps_per_scheduler"),
                      "issue_active_pct": value("scheduler", "issue_active_pct")},
        "stalls": [{"reason": item.get("reason"), "value": item.get("value")}
                   for item in (kernel.get("warp_stalls") or [])[:3]],
        "throughput_pct": {"sm": value("throughput", "sm_pct"),
                           "compute_memory": value("throughput", "compute_memory_pct"),
                           "dram": value("throughput", "dram_pct"),
                           "l1": value("throughput", "l1_pct"),
                           "l2": value("throughput", "l2_pct")},
        "memory": {"l1_hit_pct": value("memory", "l1_hit_pct"),
                   "l2_hit_pct": value("memory", "l2_hit_pct"),
                   "global_load_bytes_per_sector": value("memory", "global_load_bytes_per_sector"),
                   "global_store_bytes_per_sector": value("memory", "global_store_bytes_per_sector"),
                   "local_load_bytes_per_sector": value("memory", "local_load_bytes_per_sector"),
                   "dram_read_bytes": value("memory", "dram_read_bytes"),
                   "dram_write_bytes": value("memory", "dram_write_bytes"),
                   "spill_requests_pct": value("memory", "local_spill_requests_pct"),
                   "shared_bank_conflicts": value("memory", "shared_bank_conflicts"),
                   "l2_excessive_sectors": source.get("l2_excessive_sectors")},
        "instructions": {"executed": value("instructions", "executed"),
                         "branch_instruction_pct": value("instructions", "branch_instruction_pct"),
                         "threads_per_instruction": value("instructions", "threads_executed_per_instruction"),
                         "top_pipes": [{"pipe": item.get("pipe"), "value": item.get("value")}
                                       for item in (kernel.get("instructions") or {}).get("top_compute_pipes", [])]},
        "roofline": kernel.get("roofline"),
        "hotspot_fraction": hotspot_fraction,
        "signals": [item.get("kind") for item in (kernel.get("signals") or [])[:3]],
        "rules": [{"name": item.get("name"), "description": item.get("description"),
                   "estimated_speedup": item.get("estimated_speedup")}
                  for item in (diagnostics.get("rules") or [])[:3]],
        "source_hotspot": {key: source.get(key) for key in
                           ["address", "instruction", "samples", "not_issued_samples",
                            "dominant_not_issued_stall", "avg_threads_executed",
                            "divergent_branches", "l1_shared_conflicts",
                            "l2_excessive_sectors"]},
        "drilldowns": list((diagnostics.get("drilldowns") or {}).keys()),
    })
    capsule["advice"] = advise_kernel(capsule, hotspot_fraction, trial_counts,
                                      observed_speedup, source_features)
    return capsule


DELTA_METRICS = [
    ("duration", ("duration_ns",)),
    ("registers/thread", ("launch", "registers_per_thread")),
    ("occupancy%", ("occupancy", "achieved_pct")),
    ("eligible-warps", ("scheduler", "eligible_warps")),
    ("issue-active%", ("scheduler", "issue_active_pct")),
    ("SM%", ("throughput_pct", "sm")),
    ("DRAM%", ("throughput_pct", "dram")),
    ("L1-hit%", ("memory", "l1_hit_pct")),
    ("L2-hit%", ("memory", "l2_hit_pct")),
    ("global-load-B/sector", ("memory", "global_load_bytes_per_sector")),
    ("DRAM-read-bytes", ("memory", "dram_read_bytes")),
    ("DRAM-write-bytes", ("memory", "dram_write_bytes")),
    ("spill%", ("memory", "spill_requests_pct")),
    ("instructions", ("instructions", "executed")),
    ("threads/instruction", ("instructions", "threads_per_instruction")),
    ("arithmetic-intensity", ("roofline", "arithmetic_intensity_flop_per_byte")),
    ("achieved-GFLOP/s", ("roofline", "achieved_gflops")),
    ("roofline-efficiency%", ("roofline", "roofline_efficiency_pct")),
    ("kernel-opportunity-ceiling", ("roofline", "kernel_speedup_ceiling")),
]


def ncu_delta(baseline: dict | None, variant: dict | None) -> list[dict]:
    if not baseline or not variant:
        return []
    changes = []
    for name, path in DELTA_METRICS:
        before, after = nested(baseline, *path), nested(variant, *path)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in [before, after]):
            continue
        pct = ((after / before - 1) * 100) if before else None
        changes.append({"metric": name, "base": compact_number(before),
                        "opt": compact_number(after), "pct": compact_number(pct)})
    baseline_stalls = {item.get("reason"): item.get("value")
                       for item in baseline.get("stalls", [])}
    for item in variant.get("stalls", [])[:3]:
        reason, after = item.get("reason"), item.get("value")
        before = baseline_stalls.get(reason)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in [before, after]):
            continue
        pct = ((after / before - 1) * 100) if before else None
        changes.append({"metric": f"stall:{reason}", "base": compact_number(before),
                        "opt": compact_number(after), "pct": compact_number(pct)})
    return changes


def attach_advice(profile: dict, hotspot_fraction: float | None = None,
                  trial_counts: dict[str, int] | None = None,
                  observed_speedup: float | None = None,
                  source_features: list[str] | None = None) -> dict:
    profile = dict(profile)
    profile["advice"] = advise_kernel(profile, hotspot_fraction, trial_counts,
                                      observed_speedup, source_features)
    return profile
