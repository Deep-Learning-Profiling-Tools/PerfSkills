#!/usr/bin/env python3
"""Validate an experiment plan and own persistent run state."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from experiment_report import refresh_brief
from experiment_routing import resolve_routes
from experiment_bootstrap import ensure_baseline_profile, ensure_preflight
from experiment_support import (REPO, display, expand, expanded_env, load, resolve, sha256,
                                source_files, source_state, write)
from kernel_runner import run_kernel_profile
from profiler_contract import hydrate
from validate import errors_for

BRIEF = REPO / "shared/scripts/brief.py"


def context(plan: dict) -> tuple[Path, dict[str, str]]:
    variables = {"repo": str(REPO), "run_id": plan["run_id"]}
    run = resolve(plan["run_dir"], variables)
    variables["run_dir"] = str(run)
    return run, variables


def read_plan(path: Path) -> tuple[dict, Path, dict[str, str]]:
    plan = load(path.resolve(), {})
    schema = load(REPO / "shared/schema/experiment-plan.schema.json", {})
    errors, _ = errors_for(schema, plan)
    if errors:
        raise ValueError("invalid experiment plan: " + "; ".join(errors[:3]))
    if plan.get("version") != 1:
        raise ValueError("experiment plan must have version=1")
    run, variables = context(plan)
    source = plan.get("source") or {}
    if not source.get("baseline_dir") or not source.get("fingerprint_globs"):
        raise ValueError("source.baseline_dir and source.fingerprint_globs are required")
    execution = plan.get("execution") or {}
    if not execution.get("command"):
        raise ValueError("execution.command is required")
    iteration_profile = plan.get("iteration_profile") or {}
    if (not iteration_profile.get("baseline_profile") or
            not iteration_profile.get("profile_output") or
            not iteration_profile.get("source_targets") or
            not iteration_profile.get("stages")):
        raise ValueError("iteration_profile baseline_profile, profile_output, source_targets, and stages are required")
    kernel_profile = plan.get("kernel_profile") or {}
    if kernel_profile.get("enabled"):
        attribution = plan.get("attribution") or {}
        if not (kernel_profile.get("baseline_kernel_regex") or
                attribution.get("baseline_target_regex")):
            raise ValueError("kernel_profile needs baseline_kernel_regex or attribution.baseline_target_regex")
        if not (kernel_profile.get("variant_kernel_regex") or
                attribution.get("variant_target_regex")):
            raise ValueError("kernel_profile needs variant_kernel_regex or attribution.variant_target_regex")
    return plan, run, variables


def contract_hash(plan: dict, baseline: Path, variables: dict[str, str]) -> str:
    baseline_profile = resolve(plan["iteration_profile"]["baseline_profile"], variables)
    material = {"plan": plan,
                "baseline_source": source_state(baseline, plan["source"]["fingerprint_globs"]),
                "baseline_profile": {"path": str(baseline_profile),
                                     "sha256": sha256(baseline_profile)}}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def initial_state(plan: dict, baseline: Path, digest: str) -> dict:
    return {"protocol": "profileweaver.experiment-state/v1", "run_id": plan["run_id"],
            "contract_sha256": digest, "baseline": {"workspace": str(baseline)},
            "candidates": {}, "batches": [], "best": None, "profile_frontier": None,
            "generated_utc": datetime.now(timezone.utc).isoformat()}


def ensure_initialized(plan: dict, run: Path, variables: dict[str, str]) -> tuple[dict, Path]:
    run.mkdir(parents=True, exist_ok=True)
    baseline = resolve(plan["source"]["baseline_dir"], variables)
    if not baseline.is_dir():
        raise ValueError(f"baseline source directory is missing: {baseline}")
    if not source_files(baseline, plan["source"]["fingerprint_globs"]):
        raise ValueError("baseline fingerprint globs matched no files")
    routes = resolve_routes(plan, run)
    ensure_preflight(plan, run, variables)
    baseline_profile = ensure_baseline_profile(plan, run, baseline, variables)
    hydrate(plan, run, baseline_profile)
    digest = contract_hash(plan, baseline, variables)
    write(run / "experiment-plan.json", plan)
    state_path = run / "experiment-state.json"
    state = load(state_path)
    if state:
        if state.get("contract_sha256") != digest:
            raise ValueError("experiment contract or baseline source changed")
    else:
        state = initial_state(plan, baseline, digest)
        write(state_path, state)
        write(run / "baseline-fingerprint.json", {
            "protocol": "profileweaver.baseline-fingerprint/v1", "run_id": plan["run_id"],
            "contract_sha256": digest, "source": source_state(baseline, plan["source"]["fingerprint_globs"]),
        })
    if routes:
        state["profiler_routes"] = routes["phases"]
        save_state(run, state)
    request_path = run / "request.json"
    request = load(request_path, {}) or {}
    execution = plan.get("execution") or {}
    request_variables = variables | {"workspace": str(baseline), "id": "baseline"}
    request_defaults = {
        "run_id": plan["run_id"], "skill": "profile-weaver", "question": plan.get("question"),
        "build_command": ([expand(value, request_variables) for value in execution.get("build", [])]
                          or None),
        "baseline": [expand(value, request_variables) for value in execution.get("command", [])],
        "input": plan.get("input"),
        "environment": (expanded_env(plan.get("env", {}), request_variables) |
                        expanded_env(execution.get("env", {}), request_variables)),
        "oracle": plan.get("oracle"), "metric": plan.get("metric"),
    }
    exact = {"run_id", "skill", "build_command", "baseline", "input", "environment",
             "oracle", "metric"}
    merged_request = request | {key: value for key, value in request_defaults.items()
                                if value is not None and (key in exact or request.get(key) is None)}
    if merged_request != request:
        write(request_path, merged_request)
    kernel_profile = plan.get("kernel_profile") or {}
    if kernel_profile.get("enabled"):
        baseline_ncu = run_kernel_profile(
            plan, run, "baseline", baseline, baseline_profile, "baseline", force=False)
        state.setdefault("kernel_profiles", {})["baseline"] = baseline_ncu
        save_state(run, state)
        if baseline_ncu.get("cache") != "hit" or not (run / "agent-brief.txt").is_file():
            brief_proc = subprocess.run([sys.executable, str(BRIEF), str(run)], cwd=REPO,
                                        capture_output=True, text=True)
            raw = run / "raw/experiment"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "brief-ncu-baseline.stdout.txt").write_text(brief_proc.stdout)
            (raw / "brief-ncu-baseline.stderr.txt").write_text(brief_proc.stderr)
            if brief_proc.returncode:
                raise RuntimeError(f"baseline NCU brief failed; see {display(raw)}")
    refresh_brief(plan, run, state)
    return state, baseline


def save_state(run: Path, state: dict) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    write(run / "experiment-state.json", state)


def status(plan: dict, run: Path, variables: dict[str, str]) -> dict:
    """Return state without starting a missing baseline or profiler stage."""
    baseline_profile = resolve(plan["iteration_profile"]["baseline_profile"], variables)
    state = load(run / "experiment-state.json", {}) or {}
    if not baseline_profile.is_file() or not state:
        raw = run / "raw/baseline"
        stages = (
            ("build", raw / "build.stdout.txt"),
            ("correctness", raw / "correctness.stdout.txt"),
            ("nsys-capture", raw / "capture.nsys-rep"),
            ("nsys-export", raw / "capture.sqlite"),
            ("nsys-reduce", baseline_profile),
            ("ncu-baseline", run / "profile.ncu.baseline.json"),
        )
        stage = next((name for name, artifact in stages if not artifact.is_file()), "finalize")
        return {"status": "initializing", "run_id": plan["run_id"],
                "stage": stage, "profile": display(baseline_profile)}
    measured = sum(1 for item in state["candidates"].values() if item.get("result"))
    return {"status": "verified" if state.get("confirmed") else "ready",
            "run_id": plan["run_id"],
            "candidates": len(state["candidates"]), "batches": len(state["batches"]),
            "measured": measured, "budget": (plan.get("budget") or {}).get("iterations"),
            "confirmed": state.get("confirmed"),
            "best": ({key: state["best"][key] for key in ["id", "improvement"]}
                     if state.get("best") else None)}
