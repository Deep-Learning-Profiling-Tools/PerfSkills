#!/usr/bin/env python3
"""Optional fresh winner replay for unresolved attribution gates."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from experiment_state import save_state
from experiment_support import REPO, candidate_dir, expand, load, resolve
from kernel_evidence import compact_ncu, ncu_delta
from kernel_runner import run_kernel_profile


def run_final_stages(plan: dict, run: Path, state: dict, campaign: dict) -> None:
    final = plan.get("finalize") or {}
    winner = campaign["final"]["winner"]
    variables = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
                 "winner": winner, "workspace": state["candidates"][winner]["workspace"],
                 "baseline_workspace": state["baseline"]["workspace"]}
    raw = run / "raw/experiment/finalize"
    raw.mkdir(parents=True, exist_ok=True)
    for stage in final.get("stages", []):
        argv = [expand(value, variables) for value in stage["argv"]]
        cwd = resolve(stage.get("cwd", str(REPO)), variables)
        outputs = [resolve(value, variables) for value in stage.get("produces", [])]
        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
        if outputs and all(output.exists() for output in outputs):
            continue
        env = os.environ.copy() | {key: expand(value, variables)
                                   for key, value in plan.get("env", {}).items()}
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                              timeout=stage.get("timeout_s"))
        (raw / f"{stage['id']}.stdout.txt").write_bytes(proc.stdout)
        (raw / f"{stage['id']}.stderr.txt").write_bytes(proc.stderr)
        missing = [str(output) for output in outputs if not output.exists()]
        if proc.returncode or missing:
            raise RuntimeError(f"final stage {stage['id']} failed; see {raw}")
    kernel_config = plan.get("kernel_profile") or {}
    if kernel_config.get("enabled"):
        configured = kernel_config.get("final_timeline_profile",
                                       "{run_dir}/profile.reprofile.json")
        timeline = resolve(configured, variables)
        if not timeline.is_file():
            timeline = candidate_dir(run, winner) / "profile/profile.json"
        final_ncu = run_kernel_profile(plan, run, "final", Path(variables["workspace"]),
                                       timeline, "reprofile", force=True)
        baseline_ncu = (state.get("kernel_profiles") or {}).get("baseline") or compact_ncu(
            load(run / "profile.ncu.baseline.json", {}))
        parent_ncu = ((state.get("candidates", {}).get(winner) or {}).get("profile") or {}).get("ncu")
        final_ncu["delta_baseline"] = ncu_delta(baseline_ncu, final_ncu)
        final_ncu["delta"] = final_ncu["delta_baseline"]
        final_ncu["delta_parent"] = ncu_delta(parent_ncu or baseline_ncu, final_ncu)
        state.setdefault("kernel_profiles", {})["final"] = final_ncu
        save_state(run, state)
    if final.get("run_finalizer"):
        proc = subprocess.run([sys.executable, str(REPO / "shared/scripts/finalize.py"), str(run)],
                              cwd=REPO, capture_output=True, text=True)
        (raw / "finalizer.stdout.txt").write_text(proc.stdout)
        (raw / "finalizer.stderr.txt").write_text(proc.stderr)
        if proc.returncode:
            raise RuntimeError(f"finalizer failed; see {raw / 'finalizer.stderr.txt'}")
