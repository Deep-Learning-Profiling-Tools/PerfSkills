#!/usr/bin/env python3
"""Measured analytical bounds used to rank GPU optimization opportunities."""

from __future__ import annotations

from experiment_support import compact_number, nested

SECTOR_BYTES = 32.0


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _roofline_to_amdahl(profile: dict, hotspot_fraction: float | None,
                        mechanism: str | None) -> dict:
    roofline = profile.get("roofline") or {}
    missing = list(roofline.get("missing") or [])
    kernel_ceiling = _number(roofline.get("kernel_speedup_ceiling"))
    if roofline.get("status") != "complete" or kernel_ceiling is None:
        missing.append("complete-roofline")
    valid_fraction = (_number(hotspot_fraction) is not None and
                      0 < hotspot_fraction <= 1)
    if not valid_fraction:
        missing.append("timing-profiler-hotspot-fraction")
    roi_ceiling = None
    if kernel_ceiling and valid_fraction:
        roi_ceiling = 1.0 / ((1.0 - hotspot_fraction) + hotspot_fraction / kernel_ceiling)
    roofline_step = {key: roofline.get(key) for key in (
        "precision", "classification", "arithmetic_intensity_flop_per_byte",
        "ridge_point_flop_per_byte", "achieved_gflops", "attainable_gflops",
        "achieved_dram_gbs", "dram_peak_gbs", "roofline_efficiency_pct")
                    if roofline.get(key) is not None}
    return {
        "name": "roofline-to-amdahl", "status": "complete" if not missing else "partial",
        "kind": "analytical-opportunity-ceiling", "confidence": (
            "medium" if roi_ceiling is not None else "unavailable"),
        "value": compact_number(roi_ceiling if roi_ceiling is not None else kernel_ceiling),
        "unit": ("whole-ROI speedup ceiling" if roi_ceiling is not None else
                 "kernel speedup ceiling" if kernel_ceiling is not None else None),
        "hierarchy": {
            "gpa_family": mechanism,
            "roofline": roofline_step or {"status": roofline.get("status", "unavailable")},
            "kernel": {"speedup_ceiling": compact_number(kernel_ceiling)},
            "application": {"hotspot_fraction": compact_number(hotspot_fraction),
                            "speedup_ceiling": compact_number(roi_ceiling)},
        },
        "missing": sorted(set(missing)),
        "interpretation": "opportunity bound for ranking; benchmark results replace it",
    }


def analytical_models(profile: dict, hotspot_fraction: float | None = None,
                      mechanism: str | None = None) -> list[dict]:
    """Return small analytical bounds derived only from measured counters."""
    models = [_roofline_to_amdahl(profile, hotspot_fraction, mechanism)]
    load_bytes = _number(nested(profile, "memory", "global_load_bytes_per_sector"))
    store_bytes = _number(nested(profile, "memory", "global_store_bytes_per_sector"))
    if load_bytes is not None and load_bytes > 0:
        models.append({
            "name": "memory-transaction-efficiency",
            "value": compact_number(min(load_bytes / SECTOR_BYTES, 1.0)),
            "unit": "useful-byte-fraction",
            "bound": compact_number(SECTOR_BYTES / load_bytes),
            "bound_unit": "ideal transaction-count reduction, not runtime speedup",
            "inputs": {"load_bytes_per_sector": compact_number(load_bytes),
                       "sector_bytes": SECTOR_BYTES},
        })
    if store_bytes is not None and store_bytes > 0:
        models.append({
            "name": "store-transaction-efficiency",
            "value": compact_number(min(store_bytes / SECTOR_BYTES, 1.0)),
            "unit": "useful-byte-fraction",
            "inputs": {"store_bytes_per_sector": compact_number(store_bytes),
                       "sector_bytes": SECTOR_BYTES},
        })
    active = _number(nested(profile, "scheduler", "active_warps"))
    eligible = _number(nested(profile, "scheduler", "eligible_warps"))
    issue = _number(nested(profile, "scheduler", "issue_active_pct"))
    if active is not None and eligible is not None and active > 0:
        models.append({
            "name": "latency-hiding",
            "value": compact_number(eligible / active),
            "unit": "eligible-per-active-warp",
            "inputs": {"active_warps": compact_number(active),
                       "eligible_warps": compact_number(eligible),
                       "issue_active_pct": compact_number(issue)},
        })
    dram_read = _number(nested(profile, "memory", "dram_read_bytes"))
    dram_write = _number(nested(profile, "memory", "dram_write_bytes"))
    duration_ns = _number(profile.get("duration_ns"))
    if duration_ns and (dram_read is not None or dram_write is not None):
        traffic = (dram_read or 0) + (dram_write or 0)
        models.append({
            "name": "observed-dram-traffic-rate",
            "value": compact_number(traffic / duration_ns),
            "unit": "GB/s",
            "inputs": {"dram_bytes": compact_number(traffic),
                       "duration_ns": compact_number(duration_ns)},
        })
    if hotspot_fraction is not None and 0 < hotspot_fraction < 1:
        models.append({
            "name": "amdahl-removal-ceiling",
            "value": compact_number(1.0 / (1.0 - hotspot_fraction)),
            "unit": "whole-ROI speedup if hotspot cost vanished",
            "inputs": {"hotspot_fraction": compact_number(hotspot_fraction)},
        })
    return models
