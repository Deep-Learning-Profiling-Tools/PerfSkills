#!/usr/bin/env python3
"""Build and cache compact per-candidate profile deltas."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from experiment_support import (REPO, candidate_dir, compact_number, display, load,
                                resolve, sha256, source_state)
from kernel_evidence import compact_ncu, ncu_delta


def profile_variables(plan: dict, run: Path, candidate_id: str, workspace: Path) -> dict[str, str]:
    profile_dir = candidate_dir(run, candidate_id) / "profile"
    values = {"repo": str(REPO), "run_id": plan["run_id"], "run_dir": str(run),
              "id": candidate_id, "workspace": str(workspace),
              "profile_phase": "baseline" if candidate_id == "baseline" else "variant",
              "profile_dir": str(profile_dir),
              "profile_raw": str(run / "raw/iteration" / candidate_id)}
    profile = plan["iteration_profile"]
    values["baseline_profile"] = str(resolve(profile["baseline_profile"], values))
    values["profile_output"] = str(resolve(profile["profile_output"], values))
    values["diff_output"] = str(profile_dir / "profile-diff.json")
    return values


def profile_signature(plan: dict, workspace: Path, values: dict[str, str]) -> str:
    dependencies = []
    for stage in plan["iteration_profile"]["stages"]:
        for value in stage.get("fingerprint_inputs", []):
            path = resolve(value, values)
            dependencies.append({"path": str(path),
                                 "sha256": sha256(path) if path.is_file() else None})
    material = {
        "contract": plan["iteration_profile"],
        "source": source_state(workspace, plan["source"]["fingerprint_globs"]),
        "baseline_profile_sha256": sha256(Path(values["baseline_profile"])),
        "dependencies": dependencies,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _metric(item: dict, name: str):
    value = item.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = (item.get("metrics") or {}).get(name)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def target_delta(plan: dict, baseline: dict, variant: dict) -> dict | None:
    attribution = plan.get("attribution") or {}
    if not attribution:
        return None
    metric = attribution.get("metric", "self_time_s")

    def total(profile: dict, pattern: str):
        expression = re.compile(pattern)
        values = [_metric(item, metric) for item in profile.get("hotspots", [])
                  if expression.search(item.get("name", ""))]
        values = [value for value in values if value is not None]
        return sum(values) if values else None

    before = total(baseline, attribution["baseline_target_regex"])
    after = total(variant, attribution["variant_target_regex"])
    change = ((after / before - 1) * 100 if before and after is not None else None)
    return {"metric": metric, "base": compact_number(before), "opt": compact_number(after),
            "pct": compact_number(change)}


def _movement(diff: dict, direction: str) -> dict | None:
    preferred = [item for item in diff.get("metric_deltas", [])
                 if item.get("metric") == "self_time_s"]
    groups = preferred or diff.get("metric_deltas", [])
    rows = [(group.get("metric"), row) for group in groups
            for row in group.get(direction, [])]
    if not rows:
        return None
    metric, row = max(rows, key=lambda pair: abs(pair[1].get("wall_impact_pct")
                                                  or pair[1].get("delta") or 0))
    result = {"name": row.get("name"), "file": row.get("file"), "line": row.get("line"),
              "metric": metric, "pct": compact_number(row.get("change_pct")),
              "wall_pct": compact_number(row.get("wall_impact_pct"))}
    return {key: value for key, value in result.items() if value is not None}


def profile_capsule(plan: dict, candidate_id: str, baseline: dict, variant: dict,
                    diff: dict, sources: dict, artifact: Path, cache: str,
                    kernel_profile: dict | None = None,
                    baseline_kernel_profile: dict | None = None,
                    parent_kernel_profile: dict | None = None) -> dict:
    remaining = diff.get("remaining_leverage") or {}
    source_target = next((item for item in sources.get("targets", [])
                          if item.get("hotspot") == remaining.get("name")), None)
    source = (source_target or {}).get("source") or {}
    source_slice = (source_target or {}).get("slice") or {}
    resolution = (source_target or {}).get("resolution") or {}
    next_target = {"name": remaining.get("name"),
                   "file": source.get("path") or remaining.get("file"),
                   "line": source.get("match_line") or remaining.get("line"),
                   "lines": ([source.get("start_line"), source.get("end_line")]
                             if source.get("start_line") and source.get("end_line") else None),
                   "slice": source_slice.get("path"), "confidence": resolution.get("confidence"),
                   "wall_pct": remaining.get("pct_of_variant_wall")}
    next_target = {key: value for key, value in next_target.items() if value is not None}
    ncu = kernel_profile or compact_ncu(variant)
    if ncu and baseline_kernel_profile:
        ncu = dict(ncu)
        ncu["delta_baseline"] = ncu_delta(baseline_kernel_profile, ncu)
        ncu["delta"] = ncu["delta_baseline"]
        ncu["delta_parent"] = ncu_delta(parent_kernel_profile or baseline_kernel_profile, ncu)
    result = {
        "id": candidate_id, "status": "complete", "cache": cache,
        "wall_pct": ((diff.get("headline") or {}).get("wall_s") or {}).get("change_pct"),
        "gpu_pct": ((diff.get("headline") or {}).get("gpu_busy_s") or {}).get("change_pct"),
        "target": target_delta(plan, baseline, variant), "gain": _movement(diff, "decreases"),
        "regression": _movement(diff, "increases"), "ncu": ncu,
        "next": next_target or None, "artifact": display(artifact),
    }
    return {key: value for key, value in result.items() if value is not None}


def cached_profile(cache_path: Path, signature: str, profile_path: Path,
                   diff_path: Path, source_path: Path) -> dict | None:
    cached = load(cache_path, {})
    if (cached.get("signature") != signature or not profile_path.is_file() or
            not diff_path.is_file() or not source_path.is_file()):
        return None
    hashes = cached.get("artifacts") or {}
    if (hashes.get("profile") != sha256(profile_path) or
            hashes.get("diff") != sha256(diff_path) or
            hashes.get("sources") != sha256(source_path)):
        return None
    capsule = cached.get("capsule")
    return capsule | {"cache": "hit"} if isinstance(capsule, dict) else None
