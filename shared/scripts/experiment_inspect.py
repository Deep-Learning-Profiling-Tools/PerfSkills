"""Bounded, named experiment evidence exposed through the single public CLI."""

from __future__ import annotations

from pathlib import Path

from agent_view_build import drilldown
from experiment_support import load
from kernel_runner import locations

GROUPS = {"launch", "occupancy", "scheduler", "stalls", "instructions", "control_flow",
          "memory", "throughput", "roofline", "rules", "source"}


def inspect(run: Path, section: str, limit: int, candidate: str | None = None,
            group: str | None = None) -> dict:
    if section == "routing":
        return load(run / "profiler-routing.json", {"status": "not-recorded"})
    if section != "ncu":
        return drilldown(run, section, limit)
    if not candidate or not group:
        raise ValueError("NCU inspection requires --candidate and --group")
    if group not in GROUPS:
        raise ValueError(f"unknown NCU group {group}; choose from {sorted(GROUPS)}")
    drilldowns = locations(run, candidate)[3]
    value = load(drilldowns / f"{group}.json")
    if not value:
        return {"status": "unavailable", "candidate": candidate, "group": group}
    value["candidate"] = candidate
    return value
