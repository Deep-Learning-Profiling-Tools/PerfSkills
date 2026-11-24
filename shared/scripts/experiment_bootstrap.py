"""Run cached preflight and create missing baseline evidence behind experiment init."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from campaign_io import check_oracle, extract, reporting_quantum
from experiment_support import REPO, display, expand, expanded_env, resolve, load, write
from profile_delta import profile_variables
from baseline_control import adaptive, capture_timeout, progress
from bounded_process import run as _run


def _verify(plan, values, env, raw, label="correctness", record=True):
    execution = plan.get("execution") or {}
    argv = [expand(item, values) for item in execution["command"]]
    cwd = resolve(execution.get("cwd", "{workspace}"), values)
    proc = _run(argv, cwd, env, raw / f"{label}.stdout.txt",
                raw / f"{label}.stderr.txt", execution.get("timeout_s"))
    stdout = (raw / f"{label}.stdout.txt").read_text(errors="replace")
    stderr = (raw / f"{label}.stderr.txt").read_text(errors="replace")
    oracle_started = time.perf_counter()
    oracle = check_oracle(plan.get("oracle"), proc.returncode, stdout, stderr, cwd, env,
                          values, raw / label)
    oracle_s = time.perf_counter() - oracle_started
    if not oracle["passed"]:
        raise RuntimeError(f"baseline correctness failed ({oracle['reason']}); see {display(raw)}")
    if (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3":
        sample = {
            "metric": extract(plan["metric"], stdout, stderr, proc.elapsed_s, values),
            "wall_s": proc.elapsed_s, "oracle": oracle, "oracle_s": oracle_s,
            "reporting_quantum": reporting_quantum(plan["metric"], stdout, stderr, values),
            "stdout": str(raw / f"{label}.stdout.txt"), "stderr": str(raw / f"{label}.stderr.txt")}
        from application_phases import parse
        sample["application_phases"] = parse(stdout)
        if record:
            from baseline_qualification import reported_facts
            write(raw.parent.parent / "baseline-timing.json", {"sample": sample, "samples": [sample],
                  "role": "unprofiled-baseline-correctness-and-timing"})
            write(raw.parent.parent / "application-facts.json", {
                "role": "application-reported-not-inferred", "stdout": sample["stdout"],
                "facts": reported_facts(stdout)})
        progress(plan, raw.parent.parent, "baseline-measurement", "complete", metric=sample["metric"],
                 unit=(plan.get("metric") or {}).get("unit"), wall_s=sample["wall_s"],
                 correctness=True, benchmark_executions=1 if record else 2,
                 baseline_stats=str(raw.parent.parent / "baseline-timing.json"))
        return sample


def ensure_preflight(plan: dict, run: Path, variables: dict[str, str]) -> None:
    value = plan.get("preflight_plan")
    if not value:
        return
    path = resolve(value, variables)
    raw = run / "raw/experiment/bootstrap"
    proc = _run([sys.executable, str(REPO / "shared/scripts/preflight.py"), str(path)],
                REPO, os.environ.copy(), raw / "preflight.stdout.txt",
                raw / "preflight.stderr.txt")
    if proc.returncode:
        raise RuntimeError(f"cached preflight failed; see {display(raw)}")


def ensure_baseline_profile(plan: dict, run: Path, baseline: Path,
                            variables: dict[str, str]) -> Path:
    profile = resolve(plan["iteration_profile"]["baseline_profile"], variables)
    if profile.is_file():
        return profile
    raw = run / "raw/baseline"
    values = profile_variables(plan, run, "baseline", baseline)
    values.update({"profile_dir": str(run), "profile_raw": str(raw),
                   "profile_output": str(profile)})
    source_targets = resolve(plan["iteration_profile"]["source_targets"], values)
    env = (os.environ.copy() | expanded_env(plan.get("env", {}), values) |
           expanded_env(plan.get("execution", {}).get("env", {}), values) |
           expanded_env(plan["iteration_profile"].get("env", {}), values))
    execution = plan.get("execution") or {}
    if execution.get("build"):
        progress(plan, run, "build", "running")
        argv = [expand(item, values) for item in execution["build"]]
        cwd = resolve(execution.get("build_cwd", "{workspace}"), values)
        proc = _run(argv, cwd, env, raw / "build.stdout.txt", raw / "build.stderr.txt",
                    execution.get("build_timeout_s"))
        if proc.returncode:
            from inspection_summary import failure
            raise RuntimeError(f"baseline build failed; {failure(raw / 'build.stderr.txt')}; see {display(raw)}")
        progress(plan, run, "build", "complete", build_s=proc.elapsed_s)
    progress(plan, run, "baseline-measurement", "running")
    _verify(plan, values, env, raw)
    stages = plan["iteration_profile"]["stages"]
    if (plan.get("profiling") or {}).get("baseline_mode") == "capability-first-v2":
        from baseline_acquisition import acquire
        stages = acquire(plan, run, baseline, profile, source_targets, raw, stages)
    for stage in stages:
        argv = [expand(item, values) for item in stage["argv"]]
        cwd = resolve(stage.get("cwd", str(REPO)), values)
        stage_env = env | expanded_env(stage.get("env", {}), values)
        timeout = stage.get("timeout_s")
        if adaptive(plan) and stage["id"].endswith("capture") and timeout is None:
            timeout = capture_timeout(plan, run)
        elif adaptive(plan) and timeout is None:
            timeout = execution.get("timeout_s", 300)
        progress(plan, run, stage["id"], "running", timeout_s=timeout)
        try:
            from resource_meter import stage_meter
            with stage_meter(plan, run, argv) as meter:
                proc = _run(argv, cwd, stage_env, raw / f"{stage['id']}.stdout.txt",
                            raw / f"{stage['id']}.stderr.txt", timeout)
                meter["usable"] = proc.returncode == 0
        except RuntimeError as exc:
            progress(plan, run, stage["id"], "failed", reason=str(exc))
            raise
        missing = [resolve(item, values) for item in stage.get("produces", [])
                   if not resolve(item, values).exists()]
        if proc.returncode or missing:
            raise RuntimeError(f"baseline profile stage {stage['id']} failed; see {display(raw)}")
        progress(plan, run, stage["id"], "complete", elapsed_s=proc.elapsed_s)
    if not profile.is_file():
        raise RuntimeError(f"baseline profiler did not produce {display(profile)}")
    if not source_targets.is_file():
        raise RuntimeError(f"baseline source locator did not produce {display(source_targets)}")
    validate = _run(
        [sys.executable, str(REPO / "shared/scripts/validate.py"),
         str(REPO / "shared/schema/profile.schema.json"), str(profile)],
        REPO, env, raw / "validate.stdout.txt", raw / "validate.stderr.txt")
    if validate.returncode:
        raise RuntimeError(f"invalid normalized baseline profile; see {display(raw)}")
    from baseline_qualification import qualify
    qualify(plan, run, lambda: _verify(plan, values, env, raw, "reference-check", record=False))
    return profile
