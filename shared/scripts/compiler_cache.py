"""Use a content-checked compiler cache for compatible isolated CMake builds."""

import os
import re
import shutil
from pathlib import Path

from experiment_support import REPO

_CMAKE = re.compile(r"(?<![\w.-])cmake(?=['\"\s]|$)")
_OUT_OF_TREE = "{run_dir}/build-{id}"
_SIBLING_BUILD = "{workspace}/../build"
_LAUNCHERS = tuple(f"CMAKE_{language}_COMPILER_LAUNCHER" for language in ("C", "CXX", "CUDA"))


def _relocate(value):
    if isinstance(value, str):
        return value.replace(_OUT_OF_TREE, _SIBLING_BUILD)
    if isinstance(value, list):
        return [_relocate(item) for item in value]
    if isinstance(value, dict):
        return {key: _relocate(item) for key, item in value.items()}
    return value


def configure(plan):
    """Keep per-snapshot outputs while giving compilations stable relative paths."""
    execution = plan.get("execution") or {}
    commands = " ".join(execution.get("build") or [])
    if not _CMAKE.search(commands) or _OUT_OF_TREE not in commands or "COMPILER_LAUNCHER" in commands:
        return plan
    inherited = os.environ | (plan.get("env") or {}) | (execution.get("env") or {})
    if any(inherited.get(key) for key in (*_LAUNCHERS, "CCACHE_DISABLE")):
        return plan
    ccache = shutil.which("ccache", path=inherited.get("PATH"))
    if not ccache:
        ccache = shutil.which(str(REPO / "runs/_cache/build-tools/ccache"))
    if not ccache:
        return plan
    launcher = str(Path(ccache).resolve())
    # CMake resolves the template from the current snapshot. The build directory
    # stays outside its source tree, so generated files never enter the patch.
    plan = _relocate(plan)
    cache_env = {
        "CCACHE_DIR": "{run_dir}/compiler-cache",
        "CCACHE_BASEDIR": "{run_dir}",
        "CCACHE_COMPILERCHECK": "content",
        **{name: launcher for name in _LAUNCHERS},
    }
    plan["env"] = (plan.get("env") or {}) | cache_env
    return plan
