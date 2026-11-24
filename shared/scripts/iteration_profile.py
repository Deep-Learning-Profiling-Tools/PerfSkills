#!/usr/bin/env python3
"""Collect and compare one correct candidate's bounded profiling evidence."""

from __future__ import annotations

from profile_sources import _locate_source

from profile_observations import timeline_observations, observation_capsule, comparison_baseline, _artifact_path, _candidate_ncu_enabled

import os
import shutil
import subprocess
import sys
from pathlib import Path

from experiment_support import (REPO, candidate_dir, display, expand, expanded_env, load,
                                resolve, sha256, write)
from kernel_runner import candidate_kernel_profile
from profile_delta import cached_profile, profile_capsule, profile_signature, profile_variables
from profile_policy import ncu_sections, timeline_for_candidate
from adaptive_policy import enabled, model_context
from acquisition_limits import bounded, limited, remaining
from bounded_process import run as run_process
from profile_dispatch import dispatch


@bounded
def run_iteration_profile(plan: dict, run: Path, candidate_id: str, row: dict,
                          parent_id: str, baseline_kernel_profile: dict | None,
                          parent_kernel_profile: dict | None,
                          force_ncu: bool = False, phase: str = "screen",
                          acquisition: dict | None = None) -> dict:
    adaptive = enabled(plan)
    if not (row.get("correctness") or {}).get("passed"):
        skipped = {"id": candidate_id, "status": "skipped-incorrect"}
        if (plan.get("kernel_profile") or {}).get("enabled"):
            skipped["ncu"] = {"status": "skipped", "disposition": "skipped",
                              "reason": "correctness-failed", "spent_s": 0}
        return skipped
    if adaptive:
        plan, result = dispatch(plan, run, candidate_id, row, parent_id, baseline_kernel_profile,
            parent_kernel_profile, force_ncu, phase, acquisition, locate=_locate_source,
            recapture=run_iteration_profile, sections=ncu_sections)
        if result is not None:
            return result
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
                        (cached_ncu.get("delta_parent") is not None or
                         cached_ncu.get("target_changed") or adaptive)))
        if ncu_current and not force_ncu:
            if cached_ncu:
                cached["ncu"] = dict(cached_ncu) | {
                    "disposition": "cache-hit", "reason": "candidate-profile-cache-valid",
                    "spent_s": 0, "previous_disposition": cached_ncu.get("disposition")}
            return cached
        baseline_path = comparison_baseline(plan, run, variables, ncu_enabled, adaptive)
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
        if kernel.get("status") not in {None, "complete"}:
            return {"id": candidate_id, "status": "blocked", "reason": kernel.get("reason"), "ncu": kernel}
        captured = _artifact_path(kernel)
        if captured.resolve() != profile_path.resolve():
            shutil.copy2(captured, profile_path)
        if kernel.get("disposition") == "reused":
            reused = load(profile_path, {})
            reused.update({"phase": "variant", "variant": candidate_id,
                           "totals": {"wall_time_s": None, "gpu_busy_s": None, "attributed_s": None},
                           "scope": {"timing_join": "context-only", "reused_from": parent_id}})
            for hotspot in reused.get("hotspots", []):
                hotspot.update({"self_time_s": None, "total_time_s": None,
                                "pct_of_wall": None, "calls": None, "amdahl_ceiling": None})
            write(profile_path, reused)
        _locate_source(run, workspace, profile_path, source_path, profile_raw)
    else:
        for stage in profile_config["stages"]:
            argv = [expand(value, variables) for value in stage["argv"]]
            cwd = resolve(stage.get("cwd", str(REPO)), variables)
            env = shared_env | expanded_env(stage.get("env", {}), variables)
            from resource_meter import stage_meter
            with stage_meter(plan, run, argv) as meter:
                proc = run_process(argv, cwd, env, profile_raw / f"{stage['id']}.stdout.txt",
                                   profile_raw / f"{stage['id']}.stderr.txt", remaining(stage.get("timeout_s")))
                meter["usable"] = proc.returncode == 0
            missing = [display(resolve(value, variables)) for value in stage.get("produces", [])
                       if not resolve(value, variables).exists()]
            if proc.returncode or missing:
                raise RuntimeError(f"iteration profile stage {stage['id']} failed for {candidate_id}; "
                                   f"see {display(profile_raw)}")
    if not profile_path.is_file():
        raise RuntimeError(f"iteration profiler did not produce {display(profile_path)}")
    if not source_path.is_file():
        raise RuntimeError(f"iteration source locator did not produce {display(source_path)}")

    normalized = load(profile_path, {})
    if normalized.get("variant") is None:
        normalized["variant"] = candidate_id
        write(profile_path, normalized)
    elif normalized["variant"] != candidate_id:
        raise RuntimeError("iteration profile names a different candidate")

    validate = subprocess.run(
        [sys.executable, str(REPO / "shared/scripts/validate.py"),
         str(REPO / "shared/schema/profile.schema.json"), str(profile_path)],
        cwd=REPO, capture_output=True, text=True, timeout=remaining(60))
    (profile_raw / "validate.stdout.txt").write_text(validate.stdout)
    (profile_raw / "validate.stderr.txt").write_text(validate.stderr)
    if validate.returncode:
        raise RuntimeError(f"invalid normalized profile for {candidate_id}; see {display(profile_raw)}")

    baseline_path = comparison_baseline(plan, run, variables, ncu_enabled, adaptive)
    diff_argv = [sys.executable, str(REPO / "shared/scripts/diff_profiles.py"),
                 "--baseline", str(baseline_path), "--variant", str(profile_path),
                 "--output", str(diff_path), "--max-items",
                 str(profile_config.get("max_delta_items", 2))]
    diff_argv += ["--source-targets", str(source_path)]
    diff_proc = subprocess.run(diff_argv, cwd=REPO, capture_output=True, text=True, timeout=remaining(60))
    (profile_raw / "diff.stdout.txt").write_text(diff_proc.stdout)
    (profile_raw / "diff.stderr.txt").write_text(diff_proc.stderr)
    diff = load(diff_path, {})
    if diff_proc.returncode or not diff:
        raise RuntimeError(f"profile delta failed for {candidate_id}; see {display(profile_raw)}")
    comparability = diff.get("comparability") or {}
    changed_provider = adaptive and not comparability.get("same_tool")
    if not changed_provider and not (comparability.get("same_tool") and
            comparability.get("all_declared_workload_fields_equal")):
        raise RuntimeError(f"profile delta is not comparable for {candidate_id}; see {display(diff_path)}")

    baseline = load(baseline_path, {})
    variant = load(profile_path, {})
    sources = load(source_path, {})
    if kernel is None and ncu_enabled:
        kernel = candidate_kernel_profile(plan, run, candidate_id, workspace, profile_path,
                                          parent_id, parent_kernel_profile, force=force_ncu)
    capsule = (observation_capsule(candidate_id, variant, sources, profile_path, diff_path) if changed_provider else
               profile_capsule(plan, candidate_id, baseline, variant, diff, sources, diff_path,
                              "miss", kernel, baseline_kernel_profile,
                              parent_kernel_profile))
    if changed_provider and kernel is not None:
        capsule["ncu"] = kernel
    if (variant.get("tool") or {}).get("name") == "nsight-systems":
        capsule["profile_artifact"] = str(profile_path)
        from profile_observations import timeline_context
        details = load(profile_dir / "roi-metrics.json", {})
        capsule["observed_capabilities"] = timeline_observations(variant, details)
        capsule["timeline"] = timeline_context(details, capsule["observed_capabilities"])
    write(cache_path, {"protocol": "profileweaver.iteration-profile-cache/v1",
                       "signature": signature, "capsule": capsule,
                       "artifacts": {"profile": sha256(profile_path), "diff": sha256(diff_path),
                                     "sources": sha256(source_path)}})
    return capsule
