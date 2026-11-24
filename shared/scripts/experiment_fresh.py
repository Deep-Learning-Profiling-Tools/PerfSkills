#!/usr/bin/env python3
"""Optional fresh winner replay for unresolved attribution gates."""

from __future__ import annotations

import os
import subprocess
import sys
import copy
import time
import shutil
from pathlib import Path

from experiment_state import save_state
from experiment_support import REPO, candidate_dir, expand, load, resolve
from kernel_evidence import compact_ncu, ncu_delta
from kernel_runner import run_kernel_profile
from adaptive_policy import enabled
from start_contract import selected_kernel


def run_final_stages(plan: dict, run: Path, state: dict, campaign: dict) -> None:
    final = plan.get("finalize") or {}
    winner = campaign["final"]["winner"]
    variables = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
                 "winner": winner, "workspace": state["candidates"][winner]["workspace"],
                 "baseline_workspace": state["baseline"]["workspace"]}
    raw = run / "raw/experiment/finalize"
    raw.mkdir(parents=True, exist_ok=True)
    costs = {"timeline_stages_s": 0, "kernel_s": 0}
    stages = final.get("stages", [])
    baseline_doc = load(run / "profile.baseline.json", {})
    if ((plan.get("profiling") or {}).get("baseline_mode") == "capability-first-v2" and
            (baseline_doc.get("tool") or {}).get("name") == "cupti-activity"):
        from cupti_activity import capture
        started = time.perf_counter()
        activity = capture(plan, run, winner, Path(variables["workspace"]), phase="reprofile")
        costs["timeline_stages_s"] = time.perf_counter() - started
        if activity.get("status") != "complete":
            raise RuntimeError("winner activity could not establish the baseline's profiling scope; see final-cupti artifacts")
        shutil.copy2(activity["profile_artifact"], run / "profile.reprofile.json")
        stages = []
    if enabled(plan):
        for stage in stages:
            for value in stage.get("produces", []):
                resolve(value, variables).unlink(missing_ok=True)
    for stage in stages:
        argv = [expand(value, variables) for value in stage["argv"]]
        cwd = resolve(stage.get("cwd", str(REPO)), variables)
        outputs = [resolve(value, variables) for value in stage.get("produces", [])]
        for output in outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
        if outputs and all(output.exists() for output in outputs):
            continue
        env = os.environ.copy() | {key: expand(value, variables)
                                   for key, value in plan.get("env", {}).items()}
        started = time.perf_counter()
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                              timeout=stage.get("timeout_s"))
        costs["timeline_stages_s"] += time.perf_counter() - started
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
        target_changed = False
        if enabled(plan):
            from kernel_identity import capture_regex
            observed = (load(timeline, {}).get("hotspots") or [{}])[0].get("name")
            target = capture_regex(observed, load(run / "compiler" / (winner + ".json"), {})) if observed else selected_kernel(load(timeline, {}))
            target_changed = target != kernel_config.get("variant_kernel_regex")
            plan = copy.deepcopy(plan)
            plan["kernel_profile"]["variant_kernel_regex"] = target
            if (plan.get("source") or {}).get("fingerprint_globs"):
                from iteration_profile import _locate_source
                started = time.perf_counter()
                _locate_source(run, Path(variables["workspace"]), timeline,
                               run / "final-profile/source-targets.json", raw)
                costs["source_s"] = time.perf_counter() - started
        started = time.perf_counter()
        final_ncu = run_kernel_profile(plan, run, "final", Path(variables["workspace"]),
                                       timeline, "reprofile", force=True)
        costs["kernel_s"] = time.perf_counter() - started
        baseline_ncu = (state.get("kernel_profiles") or {}).get("baseline") or compact_ncu(
            load(run / "profile.ncu.baseline.json", {}))
        parent_ncu = ((state.get("candidates", {}).get(winner) or {}).get("profile") or {}).get("ncu")
        final_ncu["delta_baseline"] = ncu_delta(baseline_ncu, final_ncu)
        final_ncu["delta"] = final_ncu["delta_baseline"]
        final_ncu["delta_parent"] = ncu_delta(parent_ncu or baseline_ncu, final_ncu)
        if target_changed:
            final_ncu.update({"target_changed": True, "delta_baseline": None,
                              "delta": None, "delta_parent": None,
                              "comparison_scope": "winner-dominant-kernel; different-symbol-counters-not-diffed"})
        state.setdefault("kernel_profiles", {})["final"] = final_ncu
        state["final_acquisition_cost"] = costs
        save_state(run, state)
    if final.get("run_finalizer"):
        proc = subprocess.run([sys.executable, str(REPO / "shared/scripts/finalize.py"), str(run)],
                              cwd=REPO, capture_output=True, text=True)
        (raw / "finalizer.stdout.txt").write_text(proc.stdout)
        (raw / "finalizer.stderr.txt").write_text(proc.stderr)
        if proc.returncode:
            raise RuntimeError(f"finalizer failed; see {raw / 'finalizer.stderr.txt'}")
