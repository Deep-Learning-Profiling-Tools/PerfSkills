#!/usr/bin/env python3
"""Choose profiler cadence and NCU depth from an experiment contract."""

from __future__ import annotations


SECTION_HIERARCHY = {
    "memory-transaction-waste": ["LaunchStats", "Occupancy", "SchedulerStats",
                                 "WarpStateStats", "MemoryWorkloadAnalysis", "SourceCounters"],
    "memory-latency": ["LaunchStats", "Occupancy", "SchedulerStats", "WarpStateStats",
                       "MemoryWorkloadAnalysis", "SourceCounters"],
    "memory-bandwidth": ["SpeedOfLight", "LaunchStats", "Occupancy", "SchedulerStats",
                         "MemoryWorkloadAnalysis", "SourceCounters"],
    "compute-throughput": ["SpeedOfLight", "LaunchStats", "Occupancy", "SchedulerStats",
                           "WarpStateStats", "ComputeWorkloadAnalysis", "InstructionStats"],
    "register-spill": ["LaunchStats", "Occupancy", "SchedulerStats", "WarpStateStats",
                       "MemoryWorkloadAnalysis", "SourceCounters"],
    "occupancy-resource-limit": ["LaunchStats", "Occupancy", "SchedulerStats", "WarpStateStats"],
    "scheduler-starvation": ["LaunchStats", "Occupancy", "SchedulerStats", "WarpStateStats"],
    "control-divergence": ["LaunchStats", "SchedulerStats", "WarpStateStats",
                           "InstructionStats", "SourceCounters"],
}

ROOFLINE_SECTION = "SpeedOfLight_RooflineChart"


def ncu_sections(config: dict, phase: str, force: bool = False,
                 diagnostic: dict | None = None) -> list[str]:
    """Use full anchors and a verified, diagnosis-specific candidate section set."""
    full = config.get("section", "all")
    if phase in {"baseline", "reprofile"} or force:
        value = config.get(f"{phase}_sections", full)
        return [value] if isinstance(value, str) else list(value)
    value = config.get("candidate_sections")
    if value and value != "auto":
        return [value] if isinstance(value, str) else list(value)
    verified = set(config.get("available_sections") or [])
    diagnosis = (((diagnostic or {}).get("advice") or {}).get("primary") or {}).get("name")
    selected = [name for name in SECTION_HIERARCHY.get(diagnosis, []) if name in verified]
    if selected:
        if ROOFLINE_SECTION in verified and ROOFLINE_SECTION not in selected:
            selected.append(ROOFLINE_SECTION)
        return selected
    # No verified focused set means full evidence, never guessed unsupported flags.
    value = full
    return [value] if isinstance(value, str) else list(value)


def timeline_for_candidate(plan: dict) -> bool:
    """Return whether each candidate receives an Nsight Systems timeline delta."""
    profile = plan.get("iteration_profile") or {}
    return profile.get("cadence", "baseline-final") == "every-candidate"
