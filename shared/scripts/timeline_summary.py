#!/usr/bin/env python3
"""Reduce application timeline costs into compact, benchmark-neutral facts."""

from __future__ import annotations

import re


_CALL = re.compile(r"(?<![\w:])([A-Za-z_]\w*)\s*\(")


def _api_total(api: dict, words: tuple[str, ...], field: str):
    rows = [row for name, row in api.items()
            if any(word in name.lower() for word in words)]
    return sum(row.get(field, 0) or 0 for row in rows), sum(row.get("calls", 0) or 0 for row in rows)


def _family(name: str) -> str:
    outer = name.split("(", 1)[0].rsplit("::", 1)[-1]
    body_start = name.find("<")
    if body_start >= 0:
        body = name[body_start:]
        nested = _CALL.search(body)
        if nested and nested.group(1) not in {"lambda", "instance", "operator"}:
            return nested.group(1)
    return re.sub(r"<.*$", "", outer)


def _families(profile: dict, wall_s: float) -> list[dict]:
    groups = {}
    for item in (profile.get("hotspots") or [])[:20]:
        if item.get("kind") != "gpu-kernel":
            continue
        name = _family(item.get("name") or "unknown")
        row = groups.setdefault(name, {"name": name, "time_s": 0.0, "calls": 0})
        row["time_s"] += item.get("self_time_s") or 0.0
        row["calls"] += item.get("calls") or 0
    rows = sorted(groups.values(), key=lambda row: row["time_s"], reverse=True)[:4]
    for row in rows:
        row["pct_of_wall"] = round(row["time_s"] / wall_s * 100, 2) if wall_s else None
    return rows


def summarize(profile: dict, details: dict) -> dict:
    gpu = details.get("gpu") or {}
    api = details.get("host_cuda_api") or {}
    sync_s, sync_calls = _api_total(api, ("synchronize", "streamwait", "eventwait"), "thread_time_s")
    copy_s, copy_calls = _api_total(api, ("memcpy", "memset"), "thread_time_s")
    scope = details.get("scope") or {}
    copy_scopes = scope.get("copies") or {}
    copy_bytes = sum((copy_scopes.get(name) or {}).get("bytes", 0) or 0
                     for name in ("before_roi", "inside_roi", "after_roi"))
    wall_s = ((details.get("roi") or {}).get("wall_s") or
              ((profile.get("totals") or {}).get("wall_time_s")) or 0)
    return {
        "kernel_launches": gpu.get("kernel_launches"),
        "synchronizations": sync_calls or None,
        "synchronization_time_s": sync_s or None,
        "copy_calls": copy_calls or None,
        "copy_time_s": copy_s or None,
        "copy_bytes": copy_bytes or None,
        "hotspot_families": _families(profile, wall_s),
    }
