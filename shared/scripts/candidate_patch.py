"""Exact source patches and numeric-change classification."""

import difflib
import re
from pathlib import Path
from experiment_support import source_files

NUMBER = re.compile(r"(?<![A-Za-z_])(?:0x[0-9A-Fa-f]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)(?![A-Za-z_])")


def readable(path: Path) -> list[str] | None:
    try:
        return path.read_text().splitlines(keepends=True)
    except (UnicodeDecodeError, OSError):
        return None


def make_patch(parent: Path, workspace: Path, patterns: list[str], output: Path) -> str:
    left = {str(path.relative_to(parent)): path for path in source_files(parent, patterns)}
    right = {str(path.relative_to(workspace)): path for path in source_files(workspace, patterns)}
    chunks = []
    for name in sorted(set(left) | set(right)):
        before = readable(left[name]) if name in left else []
        after = readable(right[name]) if name in right else []
        if before is None or after is None or before == after:
            continue
        chunks.extend(difflib.unified_diff(before, after,
                      fromfile=f"parent/{name}", tofile=f"candidate/{name}", n=3))
    text = "".join(chunks)
    output.write_text(text)
    return text


def numeric_only_patch(text: str) -> bool:
    """Classify a pure constant sweep for reporting; never reject it."""
    removed, added = [], []
    for line in text.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(NUMBER.sub("#", line[1:].strip()))
        elif line.startswith("+"):
            added.append(NUMBER.sub("#", line[1:].strip()))
    return bool(removed and added and sorted(removed) == sorted(added))

