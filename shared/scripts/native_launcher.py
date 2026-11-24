"""Resolve a native application and preserve its MPI or scheduler launcher."""

import os
from pathlib import Path
from experiment_support import expand, resolve


def executable(plan: dict, workspace: Path, variables: dict, configured=None) -> Path | None:
    execution = plan["execution"]
    names = ([configured] if configured else []) + list(
        (plan.get("kernel_profile") or {}).get("fingerprint_inputs") or []) + execution["command"]
    cwd = resolve(execution.get("cwd", "{workspace}"), variables)
    for raw in names:
        path = Path(expand(raw, variables))
        path = path if path.is_absolute() else cwd / path
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        if not configured and not path.resolve().is_relative_to(workspace.resolve()):
            continue
        with path.open("rb") as stream:
            if stream.read(4) == b"\x7fELF":
                return path
    return None


def instrument(command: list[str], binary: Path, prefix: list[str], cwd: Path) -> list[str]:
    """Keep MPI/scheduler arguments and place hpcrun around the application."""
    for index, token in enumerate(command):
        path = Path(token)
        path = path if path.is_absolute() else cwd / path
        if path.is_file() and path.resolve() == binary.resolve():
            return command[:index] + prefix + command[index:]
    raise ValueError("cannot place profiler around the declared application inside its launcher")

