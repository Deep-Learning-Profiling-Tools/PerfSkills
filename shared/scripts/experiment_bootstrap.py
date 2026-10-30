"""Run cached preflight and create missing baseline evidence behind experiment init."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from campaign_io import check_oracle
from experiment_support import REPO, display, expand, expanded_env, resolve
from profile_delta import profile_variables


def _run(argv, cwd, env, stdout, stderr, timeout=None):
    stdout.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    stdout.write_bytes(proc.stdout)
    stderr.write_bytes(proc.stderr)
    return proc


def _verify(plan, values, env, raw):
    execution = plan.get("execution") or {}
    argv = [expand(item, values) for item in execution["command"]]
    cwd = resolve(execution.get("cwd", "{workspace}"), values)
    proc = _run(argv, cwd, env, raw / "correctness.stdout.txt",
                raw / "correctness.stderr.txt", execution.get("timeout_s"))
    stdout = (raw / "correctness.stdout.txt").read_text(errors="replace")
    stderr = (raw / "correctness.stderr.txt").read_text(errors="replace")
    oracle = check_oracle(plan.get("oracle"), proc.returncode, stdout, stderr, cwd, env,
                          values, raw / "correctness")
    if not oracle["passed"]:
        raise RuntimeError(f"baseline correctness failed ({oracle['reason']}); see {display(raw)}")


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
        argv = [expand(item, values) for item in execution["build"]]
        cwd = resolve(execution.get("build_cwd", "{workspace}"), values)
        proc = _run(argv, cwd, env, raw / "build.stdout.txt", raw / "build.stderr.txt",
                    execution.get("build_timeout_s"))
        if proc.returncode:
            raise RuntimeError(f"baseline build failed; see {display(raw)}")
    _verify(plan, values, env, raw)
    for stage in plan["iteration_profile"]["stages"]:
        argv = [expand(item, values) for item in stage["argv"]]
        cwd = resolve(stage.get("cwd", str(REPO)), values)
        stage_env = env | expanded_env(stage.get("env", {}), values)
        proc = _run(argv, cwd, stage_env, raw / f"{stage['id']}.stdout.txt",
                    raw / f"{stage['id']}.stderr.txt", stage.get("timeout_s"))
        missing = [resolve(item, values) for item in stage.get("produces", [])
                   if not resolve(item, values).exists()]
        if proc.returncode or missing:
            raise RuntimeError(f"baseline profile stage {stage['id']} failed; see {display(raw)}")
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
    return profile
