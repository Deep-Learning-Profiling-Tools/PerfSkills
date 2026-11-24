#!/usr/bin/env python3
"""Keep Claude on ProfileWeaver's compact, public experiment interface."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path


RAW_MARKERS = (
    "/raw/",
    ".ncu-rep",
    ".nsys-rep",
    ".sqlite",
    "/ncu-drilldowns/",
    "/kernel-diagnostics.json",
)
INTERNAL_MARKERS = (
    "/shared/schema/",
    "/.claude/skills/",
    "/profileweaver.json",
    "/preflight-plan.json",
    "/experiment-state.json",
)
INLINE_PYTHON = re.compile(
    r"\bpython(?:3(?:\.\d+)?)?\s+(?:-c\b|-\s*<<|<<)", re.IGNORECASE
)
DIRECT_PROFILER = re.compile(
    r"(?:^|[;&|]\s*|\b(?:sudo|command|exec|time)\s+)"
    r"(?:env\s+)?(?:[A-Za-z_]\w*=\S+\s+)*(?:\S*/)?(?:ncu|nsys)\b"
    r"|\b(?:mpirun|mpiexec|srun)\b[^\n;&|]*\b(?:ncu|nsys)\b",
    re.IGNORECASE | re.MULTILINE,
)
MUTATING_SHELL = re.compile(
    r"(?:\bsed\s+-i\b|\b(?:rm|mv|cp|install)\b|\btee\b|>>?|\bcat\b[^\n]*<<)"
)


def marker_for(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"profileweaver-claude-{digest}"


def normalized_path(value: str, cwd: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return "/" + str(path.resolve(strict=False)).replace("\\", "/").lstrip("/")


def is_candidate_source(path: str) -> bool:
    return bool(re.search(r"/runs/[^/]+/candidates/[^/]+/source(?:/|$)", path))


def blocked_marker(value: str, markers: tuple[str, ...]) -> bool:
    lowered = value.lower().replace("\\", "/")
    return any(marker in lowered for marker in markers)


def deny(reason: str) -> None:
    print(f"ProfileWeaver guard: {reason}", file=sys.stderr)
    raise SystemExit(2)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return

    session_id = str(event.get("session_id") or event.get("transcript_path") or "")
    if not session_id:
        return
    marker = marker_for(session_id)
    tool = str(event.get("tool_name") or "")
    inputs = event.get("tool_input") or {}
    command = str(inputs.get("command") or "")
    skill = str(inputs.get("skill") or inputs.get("name") or "")

    activating = (
        tool == "Skill" and skill == "profile-weaver"
    ) or (
        tool == "Bash"
        and re.search(r"shared/scripts/experiment\.py\s+start\b", command)
    )
    if activating:
        marker.touch(exist_ok=True)
    if not marker.exists() or tool == "Skill":
        return

    cwd = str(event.get("cwd") or ".")
    if tool in {"Read", "Edit", "Write"}:
        value = str(inputs.get("file_path") or inputs.get("path") or "")
        path = normalized_path(value, cwd)
        if tool in {"Edit", "Write"} and not is_candidate_source(path):
            deny("edit only the candidate source workspace returned by experiment.py")
        if blocked_marker(path, RAW_MARKERS):
            deny("use experiment.py inspect for profiler evidence; do not read raw reports")
        if tool == "Read" and blocked_marker(path, INTERNAL_MARKERS):
            deny("use the public experiment.py interface instead of backend files")
        if tool == "Read" and "/shared/scripts/" in path:
            deny("do not inspect framework Python during an optimization journey")
        return

    if tool != "Bash":
        return
    lowered = command.lower().replace("\\", "/")
    if INLINE_PYTHON.search(command):
        deny("inline Python is disabled; use experiment.py or the Edit tool")
    if DIRECT_PROFILER.search(command):
        deny("invoke profiling through experiment.py, not ncu or nsys directly")
    if blocked_marker(lowered, RAW_MARKERS):
        deny("use experiment.py inspect for profiler evidence; do not read raw reports")

    scripts = re.findall(r"shared/scripts/([A-Za-z0-9_.-]+\.py)", command)
    if any(script not in {"experiment.py", "tokens.py"} for script in scripts):
        deny("invoke only experiment.py; tokens.py is allowed for final cost reporting")
    controller = "shared/scripts/experiment.py" in lowered
    token_report = "shared/scripts/tokens.py" in lowered
    if controller or token_report:
        return
    if blocked_marker(lowered, INTERNAL_MARKERS):
        deny("use the public experiment.py interface instead of backend files")
    if MUTATING_SHELL.search(command) and not is_candidate_source(lowered):
        deny("shell writes are limited to a returned candidate source workspace")


if __name__ == "__main__":
    main()
