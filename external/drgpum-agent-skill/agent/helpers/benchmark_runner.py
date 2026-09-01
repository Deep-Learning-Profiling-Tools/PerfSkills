"""
Run a workload under DrGPUM and return the profile directory.

    from agent.helpers.benchmark_runner import profile

    out = profile(["python3", "train.py"], label="baseline")
    # -> Path("runs/baseline/gvprof-measurements/memory_liveness")

`gvprof` must be on PATH along with hpcrun/hpcstruct/hpcprof and the redshow
binaries. On a machine where DrGPUM was installed to $PREFIX:

    export DrGPUM_PATH=$PREFIX
    export PATH=$DrGPUM_PATH/bin:$DrGPUM_PATH/hpctoolkit/bin:$DrGPUM_PATH/redshow/bin:$PATH

Profiling is slow -- every device memory operation is intercepted. A PyTorch
training loop that runs in seconds unprofiled can take minutes, and the
`hpcprof` correlation pass afterwards can take considerably longer than the run
itself. Profile the smallest workload that still exhibits the behaviour you
care about: the same allocations happen in 5 iterations as in 500.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence


class ProfilerNotFound(RuntimeError):
    """gvprof (or its HPCToolkit dependencies) are not on PATH."""


#: The analysis mode this project uses. DrGPUM also supports `redundancy`,
#: `value_pattern`, and `data_flow`, none of which report memory liveness.
DEFAULT_ANALYSIS = "memory_liveness"

#: Control knobs passed to every run. Both are off by default in DrGPUM and
#: both are required for a usable profile:
#:
#:   LIVENESS_ONGPU   without it NO ACCESS operations are recorded at all, so
#:                    every object looks unused and `optimal_memory_peak` is
#:                    reported as 0 -- i.e. the tool's headline metric is
#:                    simply absent.
#:   TORCH_ANALYSIS   without it PyTorch tensors are invisible; the objects
#:                    reported are caching-allocator segments.
#:
#: Measured on this project: enabling LIVENESS_ONGPU took optimal_memory_peak
#: from "not computed" to 44.0 MB against a 62.3 MB peak, and removed two
#: false NEVER_USED findings.
DEFAULT_CONTROL_KNOBS = (
    "HPCRUN_SANITIZER_LIVENESS_ONGPU=1",
    "HPCRUN_SANITIZER_TORCH_ANALYSIS=1",
)


def check_toolchain() -> None:
    """Fail early and clearly if the profiler is not usable."""
    missing = [t for t in ("gvprof", "hpcrun", "hpcstruct", "hpcprof") if not shutil.which(t)]
    if missing:
        raise ProfilerNotFound(
            f"not on PATH: {', '.join(missing)}. Source the DrGPUM environment "
            f"first (see this module's docstring). If hpcrun is missing but "
            f"gvprof is present, HPCToolkit was never built -- `gvprof` is a "
            f"shell script that calls hpcrun, so it will appear to run and then "
            f"produce nothing."
        )


def profile(
    command: Sequence[str],
    label: str = "baseline",
    runs_dir: str = "runs",
    analysis: str = DEFAULT_ANALYSIS,
    timeout: Optional[float] = None,
    env: Optional[dict] = None,
    control_knobs: Sequence[str] = DEFAULT_CONTROL_KNOBS,
) -> Path:
    """Profile `command`, returning the directory holding the analysis output.

    Raises ProfilerNotFound if the toolchain is missing, CalledProcessError if
    the workload itself fails, and FileNotFoundError if the profiler ran but
    produced no analysis directory (usually a sign the workload never touched
    the GPU).
    """
    check_toolchain()

    run_dir = Path(runs_dir) / label
    run_dir.mkdir(parents=True, exist_ok=True)

    # gvprof writes gvprof-measurements/ into the working directory, so we run
    # from run_dir to keep runs separated. That reinterprets every relative path
    # in `command` against run_dir, which silently breaks the common case
    # (`python3 train.py` becoming runs/baseline/train.py). Resolve arguments
    # that name an existing file against the *caller's* cwd first.
    command = [
        str(Path(arg).resolve()) if Path(arg).is_file() else arg for arg in command
    ]

    # gvprof refuses to overwrite these, and a stale pair silently corrupts the
    # next run's results.
    for stale in ("gvprof-measurements", "gvprof-database"):
        shutil.rmtree(run_dir / stale, ignore_errors=True)

    knobs = [x for k in control_knobs for x in ("-ck", k)]
    argv = ["gvprof", "-v", *knobs, "-e", analysis, *command]
    log = run_dir / "gvprof.log"

    with log.open("w") as fh:
        proc = subprocess.run(
            argv,
            cwd=run_dir,
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )

    out = run_dir / "gvprof-measurements" / analysis
    if not out.is_dir():
        raise FileNotFoundError(
            f"{out} was not produced (gvprof exit {proc.returncode}). "
            f"See {log}. A run that produces no analysis directory usually "
            f"means the workload made no CUDA allocations, or hpcrun failed to "
            f"attach."
        )

    # The directory appears as soon as redshow initialises, before it has
    # flushed anything -- so its existence alone proves nothing. gvprof also
    # exits 0 after an hpcrun segfault. Require actual content, and surface the
    # profiler's own log, or the caller gets "No memory objects found" from the
    # parser much later with no clue why.
    liveness = out / "memory_liveness.txt"
    if not liveness.is_file() or liveness.stat().st_size == 0:
        tail = ""
        if log.is_file():
            lines = log.read_text(errors="replace").strip().splitlines()
            tail = "\n  ".join(lines[-4:])
        raise FileNotFoundError(
            f"{out} is empty -- the profiler started but produced no data "
            f"(gvprof exit {proc.returncode}).\n"
            f"Last lines of {log}:\n  {tail}\n"
            f"Common causes: the workload failed to start (check the path), "
            f"hpcrun segfaulted, or CUPTI could not attach because another "
            f"profiler holds the GPU."
        )
    return out


def profile_pair(
    baseline_cmd: Sequence[str],
    optimized_cmd: Sequence[str],
    runs_dir: str = "runs",
    **kwargs,
) -> tuple[Path, Path]:
    """Profile before-and-after variants for comparison.

    Both must run the *same workload configuration* -- same batch size, same
    input data, same iteration count. Comparing peaks across different
    configurations measures the configuration, not the optimization.
    """
    before = profile(baseline_cmd, label="baseline", runs_dir=runs_dir, **kwargs)
    after = profile(optimized_cmd, label="optimized", runs_dir=runs_dir, **kwargs)
    return before, after


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m agent.helpers.benchmark_runner",
        description="Profile a command under DrGPUM.",
    )
    p.add_argument("--label", default="baseline")
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--analysis", default=DEFAULT_ANALYSIS)
    p.add_argument("command", nargs=argparse.REMAINDER, help="the workload, after --")
    args = p.parse_args(argv)

    cmd = [c for c in args.command if c != "--"]
    if not cmd:
        p.error("no command given (put it after --)")

    try:
        out = profile(cmd, label=args.label, runs_dir=args.runs_dir, analysis=args.analysis)
    except (ProfilerNotFound, FileNotFoundError) as e:
        print(f"error: {e}")
        return 2
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
