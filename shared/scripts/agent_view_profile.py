"""Compact normalized profile evidence for an agent packet."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def compact_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if value == 0:
        return 0
    return float(f"{value:.7g}")


def clip(text, limit=240):
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def file_ref(run: Path, path: Path):
    if not path.exists() or not path.is_file():
        return None
    try:
        rel = str(path.relative_to(run))
    except ValueError:
        rel = str(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return {"path": rel, "bytes": path.stat().st_size, "sha256_16": digest}


def hotspot_view(hotspot):
    return {
        "rank": hotspot.get("rank"), "name": clip(hotspot.get("name"), 150),
        "kind": hotspot.get("kind"), "self_s": compact_number(hotspot.get("self_time_s")),
        "wall_pct": compact_number(hotspot.get("pct_of_wall")), "calls": hotspot.get("calls"),
    }


def profile_view(profile, limit):
    if not profile:
        return None
    totals, bottleneck = profile.get("totals") or {}, profile.get("bottleneck") or {}
    return {
        "phase": profile.get("phase"),
        "wall_s": compact_number(totals.get("wall_time_s")),
        "gpu_busy_s": compact_number(totals.get("gpu_busy_s")),
        "attributed_s": compact_number(totals.get("attributed_s")),
        "bottleneck": bottleneck.get("class"), "scope": clip(bottleneck.get("scope"), 160),
        "confidence": bottleneck.get("confidence"),
        "hotspots": [hotspot_view(x) for x in (profile.get("hotspots") or [])[:limit]],
    }


def profile_diff(before, after):
    if not before or not after:
        return None
    bt, at = before.get("totals") or {}, after.get("totals") or {}
    bw, aw = bt.get("wall_time_s"), at.get("wall_time_s")
    bg, ag = bt.get("gpu_busy_s"), at.get("gpu_busy_s")
    by_name = {x.get("name"): x for x in before.get("hotspots") or []}
    emerging, growing = [], []
    for item in (after.get("hotspots") or [])[:12]:
        name, prior = item.get("name"), by_name.get(item.get("name"))
        if prior is None:
            emerging.append(clip(name, 100))
            continue
        old, new = prior.get("self_time_s"), item.get("self_time_s")
        if old and new and new > old * 1.05:
            growing.append({"name": clip(name, 90), "before_s": compact_number(old),
                            "after_s": compact_number(new)})
    return {
        "wall_change_pct": compact_number((aw / bw - 1) * 100) if bw and aw is not None else None,
        "gpu_busy_change_pct": compact_number((ag / bg - 1) * 100) if bg and ag is not None else None,
        "emerging_hotspots": emerging[:3], "growing_hotspots": growing[:3],
    }


def impact_diff_view(diff, limit):
    if not diff:
        return None
    preferred = [x for x in diff.get("metric_deltas", []) if x.get("metric") == "self_time_s"]

    def small(item):
        value = {"name": clip(item.get("name"), 55), "base": item.get("base"),
                 "opt": item.get("opt"), "change_pct": item.get("change_pct"),
                 "wall_impact_pct": item.get("wall_impact_pct")}
        if item.get("file"):
            value.update({"file": item.get("file"), "line": item.get("line")})
        return value

    selected = [{"metric": item.get("metric"),
                 "decreases": [small(x) for x in (item.get("decreases") or [])[:limit]],
                 "increases": [small(x) for x in (item.get("increases") or [])[:limit]]}
                for item in (preferred or diff.get("metric_deltas", []))[:1]]
    line_selected = [{"metric": item.get("metric"),
                      "decreases": [small(x) for x in (item.get("decreases") or [])[:limit]],
                      "increases": [small(x) for x in (item.get("increases") or [])[:limit]]}
                     for item in diff.get("top_line_deltas", [])[:2]]
    context, leverage = diff.get("context_selection") or {}, diff.get("remaining_leverage") or {}
    leverage_view = ({"name": clip(leverage.get("name"), 90), "file": leverage.get("file"),
                      "line": leverage.get("line"), "pct_of_variant_wall": leverage.get("pct_of_variant_wall"),
                      "amdahl_ceiling": leverage.get("amdahl_ceiling_if_serial_and_removable")}
                     if leverage else None)
    return {"headline": diff.get("headline"), "impact_ranked": selected,
            "top_line_deltas": line_selected, "remaining_leverage": leverage_view,
            "unmatched_counts": {"baseline": context.get("baseline_entities", 0) - context.get("matched_entities", 0),
                                 "variant": context.get("variant_entities", 0) - context.get("matched_entities", 0)},
            "line_attribution_available": context.get("line_attribution_available")}
