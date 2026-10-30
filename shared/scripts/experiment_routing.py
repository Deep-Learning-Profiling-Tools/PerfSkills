"""Resolve and persist phase-specific profiler routes during experiment initialization."""

from __future__ import annotations

from pathlib import Path

from experiment_support import REPO, write
from profiler_registry import load, select


def resolve_routes(plan: dict, run: Path) -> dict | None:
    config = plan.get("profiling") or {}
    phases = {"discovery": config.get("discovery_needs") or [],
              "diagnostic": config.get("diagnostic_needs") or []}
    if not any(phases.values()):
        return None
    registry = load(REPO / "shared/config/registry.json")
    policy = REPO / "runs/_knowledge/policy.json"
    forced = set(config.get("available") or [])
    artifacts = set(config.get("artifacts") or [])
    tools = config.get("tool_paths") or {}
    routes = {name: select(registry, needs, forced, artifacts, tools, policy,
                           bool(config.get("allow_unavailable")))
              for name, needs in phases.items() if needs}
    document = {"protocol": "profileweaver.profiler-routing/v1", "phases": routes}
    write(run / "profiler-routing.json", document)
    blocked = [name for name, route in routes.items() if route["status"] == "blocked"]
    if blocked:
        raise RuntimeError(f"no available profiler for phases: {', '.join(blocked)}")
    kernel = plan.get("kernel_profile") or {}
    diagnostic = (routes.get("diagnostic") or {}).get("selected") or {}
    if kernel.get("enabled") and diagnostic and diagnostic.get("skill") != "pw-nsight-compute":
        raise ValueError("kernel_profile.enabled conflicts with the selected diagnostic profiler")
    return document
