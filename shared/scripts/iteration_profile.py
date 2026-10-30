#!/usr/bin/env python3
"""Collect and compare one correct candidate's bounded profiling evidence."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from experiment_support import (REPO, candidate_dir, display, expand, expanded_env, load,
                                resolve, sha256, write)
from kernel_runner import candidate_kernel_profile
from profile_delta import cached_profile, profile_capsule, profile_signature, profile_variables
from profile_policy import timeline_for_candidate


def _artifact_path(value: dict) -> Path:
    path = Path(value.get("source_artifact") or value["artifact"])
    return path if path.is_absolute() else REPO / path


def _locate_source(run: Path, workspace: Path, profile: Path, output: Path,
                   raw: Path) -> None:
    argv = [sys.executable, str(REPO / "shared/scripts/locate_source.py"),
            "--profile", str(profile), "--run-dir", str(output.parent),
            "--source-root", str(workspace), "--repo-root", str(REPO),
            "--output", str(output), "--max-targets", "4",
            "--max-variants", "3", "--dependency-depth", "3", "--max-dependencies", "9"]
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
    (raw / "locate.stdout.txt").write_text(proc.stdout)
    (raw / "locate.stderr.txt").write_text(proc.stderr)
    if proc.returncode:
        raise RuntimeError(f"source dependency location failed; see {display(raw)}")


def _candidate_ncu_enabled(plan: dict, phase: str, force: bool) -> bool:
    if not (plan.get("kernel_profile") or {}).get("enabled"):
        return False
    if force or phase == "confirm":
        return True
    return (plan.get("sampling") or {}).get("mode") != "winner-confirmation"


def run_iteration_profile(plan: dict, run: Path, candidate_id: str, row: dict,
                          parent_id: str, baseline_kernel_profile: dict | None,
                          parent_kernel_profile: dict | None,
                          force_ncu: bool = False, phase: str = "screen") -> dict:
    if not (row.get("correctness") or {}).get("passed"):
        skipped = {"id": candidate_id, "status": "skipped-incorrect"}
        if (plan.get("kernel_profile") or {}).get("enabled"):
            skipped["ncu"] = {"status": "skipped", "disposition": "skipped",
                              "reason": "correctness-failed", "spent_s": 0}
        return skipped
    ncu_enabled = _candidate_ncu_enabled(plan, phase, force_ncu)
    workspace = Path(row.get("workspace") or candidate_dir(run, candidate_id) / "source")
    variables = profile_variables(plan, run, candidate_id, workspace)
    profile_path = Path(variables["profile_output"])
    diff_path = Path(variables["diff_output"])
    source_path = resolve(plan["iteration_profile"]["source_targets"], variables)
    profile_dir = Path(variables["profile_dir"])
    profile_raw = Path(variables["profile_raw"])
    if (not profile_path.is_relative_to(profile_dir) or
            not source_path.is_relative_to(profile_dir)):
        raise ValueError("iteration profile_output and source_targets must be under {profile_dir}")
    cache_path = profile_dir / "profile-cache.json"
    signature = profile_signature(plan, workspace, variables)
    cached = cached_profile(cache_path, signature, profile_path, diff_path, source_path)
    if cached:
        cached_ncu = cached.get("ncu") or {}
        ncu_current = (not ncu_enabled or
                       (cached_ncu.get("evidence_fingerprint") and
                        cached_ncu.get("delta_parent") is not None))
        if ncu_current and not force_ncu:
            if cached_ncu:
                cached["ncu"] = dict(cached_ncu) | {
                    "disposition": "cache-hit", "reason": "candidate-profile-cache-valid",
                    "spent_s": 0, "previous_disposition": cached_ncu.get("disposition")}
            return cached
        baseline_path = (run / "profile.ncu.baseline.json"
                         if ncu_enabled and not timeline_for_candidate(plan)
                         else Path(variables["baseline_profile"]))
        baseline = load(baseline_path, {})
        variant = load(profile_path, {})
        diff = load(diff_path, {})
        sources = load(source_path, {})
        kernel = (candidate_kernel_profile(plan, run, candidate_id, workspace, profile_path,
                                           parent_id, parent_kernel_profile, force=force_ncu)
                  if (plan.get("kernel_profile") or {}).get("enabled") else None)
        capsule = profile_capsule(plan, candidate_id, baseline, variant, diff, sources,
                                  diff_path, "hit", kernel, baseline_kernel_profile,
                                  parent_kernel_profile)
        write(cache_path, {"protocol": "profileweaver.iteration-profile-cache/v1",
                           "signature": signature, "capsule": capsule,
                           "artifacts": {"profile": sha256(profile_path), "diff": sha256(diff_path),
                                         "sources": sha256(source_path)}})
        return capsule

    shutil.rmtree(profile_dir, ignore_errors=True)
    shutil.rmtree(profile_raw, ignore_errors=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_raw.mkdir(parents=True, exist_ok=True)
    profile_config = plan["iteration_profile"]
    shared_env = (os.environ.copy() | expanded_env(plan.get("env", {}), variables) |
                  expanded_env(plan.get("execution", {}).get("env", {}), variables) |
                  expanded_env(profile_config.get("env", {}), variables))
    kernel = None
    if ncu_enabled and not timeline_for_candidate(plan):
        timeline = Path(variables["baseline_profile"])
        kernel = candidate_kernel_profile(plan, run, candidate_id, workspace, timeline,
                                          parent_id, parent_kernel_profile, force=force_ncu)
        captured = _artifact_path(kernel)
        if captured.resolve() != profile_path.resolve():
            shutil.copy2(captured, profile_path)
        _locate_source(run, workspace, profile_path, source_path, profile_raw)
    else:
        for stage in profile_config["stages"]:
            argv = [expand(value, variables) for value in stage["argv"]]
            cwd = resolve(stage.get("cwd", str(REPO)), variables)
            env = shared_env | expanded_env(stage.get("env", {}), variables)
            proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                  timeout=stage.get("timeout_s"))
            (profile_raw / f"{stage['id']}.stdout.txt").write_bytes(proc.stdout)
            (profile_raw / f"{stage['id']}.stderr.txt").write_bytes(proc.stderr)
            missing = [display(resolve(value, variables)) for value in stage.get("produces", [])
                       if not resolve(value, variables).exists()]
            if proc.returncode or missing:
                raise RuntimeError(f"iteration profile stage {stage['id']} failed for {candidate_id}; "
                                   f"see {display(profile_raw)}")
    if not profile_path.is_file():
        raise RuntimeError(f"iteration profiler did not produce {display(profile_path)}")
    if not source_path.is_file():
        raise RuntimeError(f"iteration source locator did not produce {display(source_path)}")

    validate = subprocess.run(
        [sys.executable, str(REPO / "shared/scripts/validate.py"),
         str(REPO / "shared/schema/profile.schema.json"), str(profile_path)],
        cwd=REPO, capture_output=True, text=True)
    (profile_raw / "validate.stdout.txt").write_text(validate.stdout)
    (profile_raw / "validate.stderr.txt").write_text(validate.stderr)
    if validate.returncode:
        raise RuntimeError(f"invalid normalized profile for {candidate_id}; see {display(profile_raw)}")

    baseline_path = (run / "profile.ncu.baseline.json"
                     if ncu_enabled and not timeline_for_candidate(plan)
                     else Path(variables["baseline_profile"]))
    diff_argv = [sys.executable, str(REPO / "shared/scripts/diff_profiles.py"),
                 "--baseline", str(baseline_path), "--variant", str(profile_path),
                 "--output", str(diff_path), "--max-items",
                 str(profile_config.get("max_delta_items", 2))]
    diff_argv += ["--source-targets", str(source_path)]
    diff_proc = subprocess.run(diff_argv, cwd=REPO, capture_output=True, text=True)
    (profile_raw / "diff.stdout.txt").write_text(diff_proc.stdout)
    (profile_raw / "diff.stderr.txt").write_text(diff_proc.stderr)
    diff = load(diff_path, {})
    if diff_proc.returncode or not diff:
        raise RuntimeError(f"profile delta failed for {candidate_id}; see {display(profile_raw)}")
    comparability = diff.get("comparability") or {}
    if not (comparability.get("same_tool") and
            comparability.get("all_declared_workload_fields_equal")):
        raise RuntimeError(f"profile delta is not comparable for {candidate_id}; see {display(diff_path)}")

    baseline = load(baseline_path, {})
    variant = load(profile_path, {})
    sources = load(source_path, {})
    if kernel is None and ncu_enabled:
        kernel = candidate_kernel_profile(plan, run, candidate_id, workspace, profile_path,
                                          parent_id, parent_kernel_profile, force=force_ncu)
    capsule = profile_capsule(plan, candidate_id, baseline, variant, diff, sources, diff_path,
                              "miss", kernel, baseline_kernel_profile,
                              parent_kernel_profile)
    write(cache_path, {"protocol": "profileweaver.iteration-profile-cache/v1",
                       "signature": signature, "capsule": capsule,
                       "artifacts": {"profile": sha256(profile_path), "diff": sha256(diff_path),
                                     "sources": sha256(source_path)}})
    return capsule
