#!/usr/bin/env python3
"""Turn bounded NCU evidence into a compact GPA-style diagnosis."""

from __future__ import annotations

from experiment_support import compact_number, nested
from performance_models import SECTOR_BYTES, analytical_models


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rule_names(profile: dict) -> list[str]:
    return [str(item.get("name") or "").lower() for item in profile.get("rules", [])]


def _stall_map(profile: dict) -> dict[str, float]:
    return {str(item.get("reason")): item.get("value") for item in profile.get("stalls", [])
            if _number(item.get("value")) is not None}


def _families(profile: dict) -> list[dict]:
    stalls = _stall_map(profile)
    rules = _rule_names(profile)
    load_bytes = _number(nested(profile, "memory", "global_load_bytes_per_sector"))
    dram = _number(nested(profile, "throughput_pct", "dram"))
    sm = _number(nested(profile, "throughput_pct", "sm"))
    eligible = _number(nested(profile, "scheduler", "eligible_warps"))
    issue = _number(nested(profile, "scheduler", "issue_active_pct"))
    occupancy = _number(nested(profile, "occupancy", "achieved_pct"))
    theoretical = _number(nested(profile, "occupancy", "theoretical_pct"))
    registers = _number(nested(profile, "launch", "registers_per_thread"))
    spill = _number(nested(profile, "memory", "spill_requests_pct"))
    threads = _number(nested(profile, "instructions", "threads_per_instruction"))
    result = []

    def add(name, score, evidence, confidence="high"):
        result.append({"name": name, "score": score, "confidence": confidence,
                       "evidence": evidence})

    uncoalesced_rule = any("uncoales" in name or "cacheaccesspattern" in name for name in rules)
    if (load_bytes is not None and load_bytes < 16) or uncoalesced_rule:
        efficiency = load_bytes / SECTOR_BYTES if load_bytes is not None else None
        add("memory-transaction-waste", 100 if efficiency is not None and efficiency < .25 else 92,
            {"load_B_per_32B_sector": compact_number(load_bytes),
             "ncu_rule": uncoalesced_rule})
    long_scoreboard = max((value for name, value in stalls.items()
                           if "long_scoreboard" in name or "mio" in name), default=None)
    if long_scoreboard is not None and ((eligible is not None and eligible < 1) or long_scoreboard >= 10):
        add("memory-latency", 90, {"long_scoreboard": compact_number(long_scoreboard),
                                   "eligible_warps": compact_number(eligible)})
    if dram is not None and dram >= 70:
        add("memory-bandwidth", 88, {"dram_throughput_pct": compact_number(dram)})
    if sm is not None and sm >= 70 and (dram is None or dram < 60):
        add("compute-throughput", 82, {"sm_throughput_pct": compact_number(sm),
                                       "dram_throughput_pct": compact_number(dram)})
    if spill is not None and spill > 0:
        add("register-spill", 86, {"spill_requests_pct": compact_number(spill),
                                    "registers_per_thread": compact_number(registers)})
    if (occupancy is not None and theoretical is not None and theoretical - occupancy >= 15):
        add("occupancy-resource-limit", 68,
            {"achieved_pct": compact_number(occupancy),
             "theoretical_pct": compact_number(theoretical),
             "registers_per_thread": compact_number(registers)}, "medium")
    if eligible is not None and eligible < 1 and issue is not None and issue < 60:
        add("scheduler-starvation", 78, {"eligible_warps": compact_number(eligible),
                                         "issue_active_pct": compact_number(issue)})
    if ((threads is not None and threads < 28) or
            any("diverg" in name or "branch" in name for name in rules)):
        add("control-divergence", 75, {"threads_per_instruction": compact_number(threads)})
    if not result:
        add("unclassified", 10, {"available_metric_count": profile.get("metric_count")}, "low")
    return sorted(result, key=lambda item: item["score"], reverse=True)


def advise_kernel(profile: dict | None, hotspot_fraction: float | None = None,
                  trial_counts: dict[str, int] | None = None,
                  observed_speedup: float | None = None,
                  source_features: list[str] | None = None) -> dict | None:
    """Build only measured classifications and analytical context.

    Candidate mechanisms and opportunity rankings are intentionally not emitted.
    """
    if not profile:
        return None
    families = _families(profile)
    primary = families[0]
    models = analytical_models(profile, hotspot_fraction, primary["name"])
    source = profile.get("source_hotspot") or {}
    return {
        "protocol": "profileweaver.kernel-advice/v1",
        "primary": primary,
        "alternatives": families[1:3],
        "hierarchy": [
            {"level": "instruction", "fact": source or None},
            {"level": "kernel", "fact": {"family": primary["name"],
                                            "evidence": primary["evidence"]}},
            {"level": "function-data", "fact": {
                "required_context": ["hotspot", "callee", "accessed-object",
                                     "declaration", "builder-or-writer", "transfer"],
                "policy": "follow the measured dependency chain; do not browse unrelated files"}},
        ],
        "models": models,
        "candidate_authority": "agent",
    }
