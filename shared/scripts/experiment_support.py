#!/usr/bin/env python3
"""Small shared helpers for ProfileWeaver experiment modules."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expand(value: str, variables: dict[str, str]) -> str:
    return value.format_map(variables)


def resolve(value: str, variables: dict[str, str]) -> Path:
    path = Path(expand(value, variables))
    return path if path.is_absolute() else REPO / path


def expanded_env(values: dict, variables: dict[str, str]) -> dict[str, str]:
    return {str(key): expand(str(value), variables) for key, value in values.items()}


def display(path: Path | str) -> str:
    path = Path(path)
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def source_files(root: Path, patterns: list[str]) -> list[Path]:
    files = []
    for pattern in patterns:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(files))


def source_state(root: Path, patterns: list[str]) -> list[dict]:
    return [{"path": str(path.relative_to(root)), "bytes": path.stat().st_size,
             "sha256": sha256(path)} for path in source_files(root, patterns)]


def evidence_path_state(path: Path, workspace: Path) -> dict:
    """Fingerprint a declared profiler input without exposing its contents."""
    try:
        label = "{workspace}/" + str(path.resolve().relative_to(workspace.resolve()))
    except (OSError, ValueError):
        label = display(path)
    if not path.exists():
        return {"path": label, "state": "missing"}
    if path.is_file():
        return {"path": label, "state": "file", "bytes": path.stat().st_size,
                "sha256": sha256(path)}
    files = [item for item in path.rglob("*")
             if item.is_file() and "__pycache__" not in item.parts]
    return {"path": label, "state": "directory",
            "files": [{"path": str(item.relative_to(path)), "bytes": item.stat().st_size,
                       "sha256": sha256(item)} for item in sorted(files)]}


def candidate_dir(run: Path, candidate_id: str) -> Path:
    return run / "candidates" / candidate_id


def compact_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    return 0 if value == 0 else float(f"{value:.6g}")


def nested(value: dict, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
