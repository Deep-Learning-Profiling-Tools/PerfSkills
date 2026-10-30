"""Create a measured experiment from one workload directory.

The start path loads or infers the workload contract, snapshots the baseline,
generates internal plans, then delegates cached preflight and baseline profiling
to ``experiment_state.ensure_initialized``.  It returns only the plan and the
bounded evidence files needed by the agent.
"""

from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from experiment_state import ensure_initialized, read_plan
from experiment_support import REPO, display, write
from start_contract import experiment, preflight, substitute, tools
from start_manifest import load_manifest


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "workload"


def gpu_facts(found: dict[str, str]) -> dict[str, str]:
    import subprocess
    proc = subprocess.run([found["nvidia-smi"], "--query-gpu=compute_cap",
                           "--format=csv,noheader"], capture_output=True, text=True)
    arch = (proc.stdout.splitlines() or [""])[0].strip().replace(".", "")
    if proc.returncode or not arch.isdigit():
        raise ValueError("could not determine CUDA compute capability")
    host = shutil.which("g++")
    if not host:
        raise ValueError("required command is unavailable: g++")
    return {"cuda_arch": arch, "host_cxx": str(Path(host).resolve()), "repo": str(REPO)}


def run(argv: list[str]) -> dict:
    """Execute ``experiment.py start`` and return its compact JSON response."""
    parser = argparse.ArgumentParser(prog="experiment.py start")
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--repairs", type=int, default=7)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.repairs < 1:
        raise ValueError("iterations and repairs must be positive")
    # 1. Resolve the workload contract and create an immutable baseline snapshot.
    manifest, manifest_path, source = load_manifest(args.workload)
    run_id = args.run_id or (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") +
                             slug(manifest["name"]))
    run_dir = REPO / "runs" / run_id
    baseline = run_dir / "baseline-source"
    if run_dir.exists() and not (run_dir / "start-contract.json").is_file():
        raise ValueError(f"run directory already exists and is not resumable: {display(run_dir)}")
    if not baseline.exists():
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, baseline, ignore=shutil.ignore_patterns(
            *manifest.get("copy_ignore", [])), dirs_exist_ok=True)
    # 2. Resolve tools once and generate the controller-owned plans.
    found = tools()
    resolved = substitute(manifest, gpu_facts(found))
    preflight_plan = preflight(run_id, run_dir, source, found)
    plan = experiment(resolved, run_id, run_dir, baseline, found,
                      args.iterations, args.repairs)
    write(run_dir / "preflight-plan.json", preflight_plan)
    write(run_dir / "experiment-plan.json", plan)
    write(run_dir / "start-contract.json", {"protocol": "profileweaver.start/v1",
          "manifest": str(manifest_path) if manifest_path else None,
          "source": str(source), "iterations": args.iterations,
          "repairs": args.repairs})
    # 3. Build, validate, profile, reduce, and locate bounded source ranges.
    plan, run_dir, variables = read_plan(run_dir / "experiment-plan.json")
    state, _ = ensure_initialized(plan, run_dir, variables)
    # 4. Expose compact evidence paths; detailed profiler output remains under raw/.
    return {"status": "initialized", "run_id": run_id,
            "controller": display(run_dir / "experiment-plan.json"),
            "profile": display(run_dir / "agent-brief.txt"),
            "source": display(run_dir / "source-targets.json"),
            "read_only": [display(run_dir / "agent-brief.txt"),
                          display(run_dir / "source-slices")],
            "edit_only": display(run_dir / "candidates/<id>/source"),
            "kernel": (((state.get("kernel_profiles") or {}).get("baseline") or {}).get("kernel"))}
