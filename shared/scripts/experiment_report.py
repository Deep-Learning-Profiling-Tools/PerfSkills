#!/usr/bin/env python3
"""Refresh the single compact brief consumed by the optimization agent."""

from __future__ import annotations

import json
from pathlib import Path

from brief_builder import build


def refresh_brief(plan: dict, run: Path, state: dict) -> None:
    """Keep one state-aware brief; campaign state is the durable history."""
    machine, text = build(run)
    (run / "agent-brief.json").write_text(json.dumps(machine, indent=2) + "\n")
    (run / "agent-brief.txt").write_text(text)
    stale = run / "experiment-brief.txt"
    if stale.exists():
        stale.unlink()
