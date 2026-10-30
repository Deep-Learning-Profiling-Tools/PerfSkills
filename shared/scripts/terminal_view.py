#!/usr/bin/env python3
"""Bounded terminal views for iterative profiling evidence."""

from __future__ import annotations

from experiment_support import nested


def _delta_summary(rows: list[dict] | None) -> dict | None:
    values = [item for item in (rows or []) if item.get("pct") is not None]
    values.sort(key=lambda item: abs(item.get("pct") or 0), reverse=True)
    return {item["metric"]: item.get("pct") for item in values[:5]} or None


def terminal_ncu(value: dict | None) -> dict | None:
    if not value or value.get("status") not in {None, "complete"}:
        return value
    disposition = value.get("disposition") or value.get("cache")
    if disposition == "reused":
        return {"profile": "reused", "reason": "kernel-unchanged", "spent_s": 0,
                "from": value.get("reused_from"), "delta_base": "unchanged",
                "delta_parent": "unchanged", "artifact": value.get("artifact")}
    if disposition == "cache-hit" and value.get("previous_disposition"):
        return {"profile": "cache-hit", "reason": "profile-cache-valid", "spent_s": 0,
                "evidence_s": value.get("total_s"),
                "delta_base_pct": _delta_summary(value.get("delta_baseline") or value.get("delta")),
                "delta_parent_pct": _delta_summary(value.get("delta_parent")),
                "artifact": value.get("artifact")}
    stalls = value.get("stalls") or []
    top_stall = stalls[0] if stalls else None
    advice = value.get("advice") or {}
    primary = advice.get("primary") or {}
    memory = value.get("memory") or {}
    model = next((item for item in (advice.get("models") or [])
                  if item.get("name") == "roofline-to-amdahl"), {})
    hierarchy = model.get("hierarchy") or {}
    roofline = hierarchy.get("roofline") or {}
    reason_codes = {
        "kernel-evidence-fingerprint-changed": "kernel-changed",
        "candidate-capture-cache-valid": "capture-cache-valid",
        "agent-forced-replay": "forced-replay",
        "accepted-winner-fresh-replay": "winner-reprofile",
        "parent-kernel-evidence-unavailable": "parent-evidence-missing",
    }
    result = {
        "profile": disposition,
        "reason": reason_codes.get(value.get("reason"), value.get("reason")),
        "spent_s": value.get("spent_s"), "evidence_s": value.get("total_s"),
        "duration_ms": ((value.get("duration_ns") or 0) / 1e6
                        if value.get("duration_ns") is not None else None),
        "metrics": value.get("metric_count"),
        "regs": nested(value, "launch", "registers_per_thread"),
        "occ_pct": nested(value, "occupancy", "achieved_pct"),
        "eligible": nested(value, "scheduler", "eligible_warps"),
        "issue_pct": nested(value, "scheduler", "issue_active_pct"),
        "stall": f"{top_stall.get('reason')}:{top_stall.get('value')}" if top_stall else None,
        "load_B_sector": memory.get("global_load_bytes_per_sector"),
        "dram_read_GB": ((memory.get("dram_read_bytes") or 0) / 1e9
                         if memory.get("dram_read_bytes") is not None else None),
        "diagnosis": primary.get("name"),
        "roofline": ({"class": roofline.get("classification"),
                      "precision": roofline.get("precision"),
                      "ai": roofline.get("arithmetic_intensity_flop_per_byte"),
                      "kernel_ceiling": (hierarchy.get("kernel") or {}).get("speedup_ceiling"),
                      "roi_ceiling": (hierarchy.get("application") or {}).get("speedup_ceiling")}
                     if roofline else None),
        "gpa_alternatives": [item.get("name") for item in
                             (advice.get("alternatives") or []) if item.get("name")],
        "delta_base_pct": _delta_summary(value.get("delta_baseline") or value.get("delta")),
        "delta_parent_pct": _delta_summary(value.get("delta_parent")),
        "artifact": value.get("artifact"),
    }
    return {key: item for key, item in result.items() if item is not None}


def terminal_profile(value: dict, confirmation: bool = False) -> dict:
    if confirmation:
        keys = ("id", "status", "cache", "wall_pct")
        result = {key: value.get(key) for key in keys}
        result["target_pct"] = (value.get("target") or {}).get("pct")
        return {key: item for key, item in result.items() if item is not None}
    remaining = value.get("next") or {}
    next_target = {key: remaining.get(key) for key in ("name", "slice", "wall_pct")
                   if remaining.get(key) is not None}
    result = {key: value.get(key) for key in
              ("id", "status", "cache", "wall_pct", "gpu_pct", "target", "gain", "regression")}
    result.update({"next": next_target or None, "ncu": terminal_ncu(value.get("ncu")),
                   "artifact": value.get("artifact")})
    return {key: item for key, item in result.items() if item is not None}
