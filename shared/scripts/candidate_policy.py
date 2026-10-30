#!/usr/bin/env python3
"""Persist profiler evidence for the agent's next candidate decision."""

from __future__ import annotations

from pathlib import Path

from experiment_state import save_state
from experiment_support import load


def seed_frontier(state: dict, run: Path) -> None:
    """Persist baseline source-aware advice for the agent's next decision."""
    if state.get("profile_frontier"):
        return
    brief = load(run / "agent-brief.json", {})
    advice = (((brief.get("measurement") or {}).get("kernel_diagnostics") or {}).get("advice"))
    if advice:
        state["profile_frontier"] = {"candidate": "baseline", "ncu_advice": advice}
        save_state(run, state)
