"""Resolve where the GPA benchmark corpus and toolchain live.

Originally this skill was vendored *inside* the benchmark repo, so `SKILL_DIR.parents[2]`
(.claude/skills/<skill>/ -> repo root) happened to be the corpus root. Vendored under
ProfileWeaver's external/ that no longer holds, so the location must be stated explicitly:

    export PW_GPA_ROOT=/path/to/GPA          # holds GPA-Benchmark/... (benchmarks.json "dir")
                                             # and gpa/ (the built toolchain)

`PW_GPA_TOOLCHAIN` defaults to `$PW_GPA_ROOT/gpa`, the layout `bin/install.sh` produces and the
original scripts assumed. Set it only when a built toolchain lives apart from the corpus.

The parents[2] guess is kept purely as a fallback for a checkout still laid out the old way.
From external/ it resolves to something unrelated, which is why every caller routes failure
through `require_benchmark_dir` -- the error names the variable rather than leaving a confusing
path behind.
"""

import os
from pathlib import Path

ROOT_ENV = "PW_GPA_ROOT"
TOOLCHAIN_ENV = "PW_GPA_TOOLCHAIN"


def resolve_gpa_root(skill_dir):
    """Corpus root: benchmarks.json's "dir" values are relative to this."""
    override = os.environ.get(ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(skill_dir).resolve().parents[2]


def resolve_toolchain_root(gpa_root):
    """Parent of `hpctoolkit/bin` and `bin` (hpcrun, hpcstruct, hpcprof, gpa-advisor)."""
    override = os.environ.get(TOOLCHAIN_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(gpa_root) / "gpa"


def require_benchmark_dir(path, name, gpa_root):
    """Fail with an actionable message instead of a confusing downstream error.

    A missing benchmark directory here almost always means PW_GPA_ROOT is unset or wrong,
    not that the benchmark is misconfigured - say so directly.
    """
    path = Path(path)
    if path.is_dir():
        return path

    msg = [f"error: benchmark '{name}' not found at {path}"]
    if os.environ.get(ROOT_ENV):
        msg.append(f"  corpus root = {gpa_root}  (from ${ROOT_ENV})")
        msg.append(f"  Check that ${ROOT_ENV} points at the directory containing GPA-Benchmark/,")
        msg.append(f"  and that this benchmark's \"dir\" in assets/benchmarks.json is correct.")
    else:
        msg.append(f"  corpus root = {gpa_root}  (guessed from this skill's location)")
        msg.append(f"  ${ROOT_ENV} is not set. Point it at the GPA project root")
        msg.append(f"  (the directory holding GPA-Benchmark/ and gpa/):")
        msg.append(f"      export {ROOT_ENV}=/path/to/GPA")
    raise SystemExit("\n".join(msg))
