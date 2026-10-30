#!/usr/bin/env python3
"""Derive a conservative workload contract from standard build artifacts."""

from __future__ import annotations

import re
from pathlib import Path


SOURCE_GLOBS = [
    "**/*.c", "**/*.cc", "**/*.cpp", "**/*.cxx", "**/*.cu", "**/*.cuh",
    "**/*.h", "**/*.hh", "**/*.hpp", "Makefile", "makefile", "CMakeLists.txt",
    "**/*.cmake", "meson.build",
]
COPY_IGNORE = [
    "*.o", "*.a", "*.so", "*.d", "*.out", "__pycache__", "build", "cmake-build-*",
]


def _text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _program(root: Path, make_text: str) -> str | None:
    patterns = (
        r"(?im)^\s*(?:program|target|binary|executable)\s*[:?+]?=\s*([^\s\\]+)",
        r"(?im)^\s*add_executable\s*\(\s*([^\s\)]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, make_text)
        if match and re.fullmatch(r"[A-Za-z0-9_.+-]+", match.group(1)):
            return match.group(1)
    executables = sorted(
        item.name for item in root.iterdir()
        if item.is_file() and item.stat().st_mode & 0o111 and "." not in item.name
    )
    return executables[0] if executables else None


def _build(root: Path, make_text: str) -> list[str]:
    if (root / "Makefile").is_file() or (root / "makefile").is_file():
        command = ["make", "-j4"]
        if re.search(r"(?m)^\s*SM_VERSION\s*[:?+]?=", make_text):
            command.append("SM_VERSION={cuda_arch}")
        return command
    if (root / "CMakeLists.txt").is_file() and (root / "build").is_dir():
        return ["cmake", "--build", "build", "--parallel"]
    if (root / "meson.build").is_file() and (root / "build").is_dir():
        return ["meson", "compile", "-C", "build"]
    return ["true"]


def _command(root: Path, make_text: str, program: str) -> list[str]:
    if (root / "Makefile").is_file() or (root / "makefile").is_file():
        if re.search(r"(?m)^\s*run\s*:", make_text):
            return ["make", "run"]
    return ["{workspace}/" + program]


def infer(root: Path) -> dict:
    """Return only generic facts; no benchmark names, flags, or expected outputs."""
    root = root.resolve()
    make_path = next((root / name for name in ("Makefile", "makefile")
                      if (root / name).is_file()), None)
    build_text = _text(make_path) if make_path else _text(root / "CMakeLists.txt")
    program = _program(root, build_text)
    if not program:
        raise ValueError("could not infer an executable from standard build files")
    return {
        "version": 1,
        "name": root.name or "workload",
        "source": ".",
        "question": "Optimize the largest measured application or GPU hotspot.",
        "fingerprint_globs": SOURCE_GLOBS,
        "copy_ignore": [*COPY_IGNORE, program],
        "env": {"CPATH": "{repo}/shared/include"},
        "execution": {
            "build": _build(root, build_text),
            "build_cwd": "{workspace}",
            "command": _command(root, build_text, program),
            "cwd": "{workspace}",
            "build_timeout_s": 600,
            "timeout_s": 300,
        },
        "metric": {"name": "wall runtime", "unit": "s", "source": "wall",
                   "direction": "lower"},
        "oracle": {"type": "returncode"},
        "profiling": {"roi_name": "hpcperf:roi", "ncu_timeout_s": 1800},
    }
