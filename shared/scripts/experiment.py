#!/usr/bin/env python3
"""Own candidate workspaces, inherited plans, batched screening, and final confirmation.

The agent supplies one experiment plan and edits candidate source. This controller replaces
model-authored Python heredocs for copying trees, writing per-iteration campaign plans, polling,
diffing, reading results, maintaining the ledger, and assembling the final campaign.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NCU_CAPTURE = REPO / ".claude/skills/pw-nsight-compute/scripts/capture.py"
NCU_REDUCE = REPO / ".claude/skills/pw-nsight-compute/scripts/reduce_csv.py"
BRIEF = REPO / "shared/scripts/brief.py"


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def expand(value: str, variables: dict[str, str]) -> str:
    return value.format_map(variables)


def resolve(value: str, variables: dict[str, str]) -> Path:
    path = Path(expand(value, variables))
    return path if path.is_absolute() else REPO / path


def expanded_env(values: dict, variables: dict[str, str]) -> dict[str, str]:
    return {str(key): expand(str(value), variables) for key, value in values.items()}


def display(path: Path | str) -> str:
    """Use compact repository-relative paths at the agent boundary."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def context(plan: dict) -> tuple[Path, dict[str, str]]:
    variables = {"repo": str(REPO), "run_id": plan["run_id"]}
    run = resolve(plan["run_dir"], variables)
    variables["run_dir"] = str(run)
    return run, variables


def read_plan(path: Path) -> tuple[dict, Path, dict[str, str]]:
    plan = load(path.resolve(), {})
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


def source_files(root: Path, patterns: list[str]) -> list[Path]:
    files = []
    for pattern in patterns:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(files))


def source_state(root: Path, patterns: list[str]) -> list[dict]:
    return [{"path": str(path.relative_to(root)), "bytes": path.stat().st_size,
             "sha256": sha256(path)} for path in source_files(root, patterns)]


def evidence_path_state(path: Path, workspace: Path) -> dict:
    """Fingerprint a declared kernel input without exposing its contents."""
    try:
        label = "{workspace}/" + str(path.resolve().relative_to(workspace.resolve()))
    except (OSError, ValueError):
        label = display(path)
    if not path.exists():
        return {"path": label, "state": "missing"}
    if path.is_file():
        return {"path": label, "state": "file", "bytes": path.stat().st_size,
                "sha256": sha256(path)}
    files = [item for item in path.rglob("*")
             if item.is_file() and "__pycache__" not in item.parts]
    return {"path": label, "state": "directory",
            "files": [{"path": str(item.relative_to(path)), "bytes": item.stat().st_size,
                       "sha256": sha256(item)} for item in sorted(files)]}


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
    baseline = resolve(plan["source"]["baseline_dir"], variables)
    if not baseline.is_dir():
        raise ValueError(f"baseline source directory is missing: {baseline}")
    if not source_files(baseline, plan["source"]["fingerprint_globs"]):
        raise ValueError("baseline fingerprint globs matched no files")
    baseline_profile = resolve(plan["iteration_profile"]["baseline_profile"], variables)
    if not baseline_profile.is_file():
        raise ValueError(f"normalized baseline profile is missing: {baseline_profile}")
    digest = contract_hash(plan, baseline, variables)
    write(run / "experiment-plan.json", plan)
    state_path = run / "experiment-state.json"
    state = load(state_path)
    if state:
        if state.get("contract_sha256") != digest:
            raise ValueError("experiment contract or baseline source changed")
    else:
        run.mkdir(parents=True, exist_ok=True)
        state = initial_state(plan, baseline, digest)
        write(state_path, state)
        write(run / "baseline-fingerprint.json", {
            "protocol": "profileweaver.baseline-fingerprint/v1", "run_id": plan["run_id"],
            "contract_sha256": digest, "source": source_state(baseline, plan["source"]["fingerprint_globs"]),
        })
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


def candidate_dir(run: Path, candidate_id: str) -> Path:
    return run / "candidates" / candidate_id


def prepare(plan: dict, run: Path, variables: dict[str, str], args) -> dict:
    state, baseline = ensure_initialized(plan, run, variables)
    if not ID_PATTERN.match(args.id) or args.id == "baseline":
        raise ValueError("candidate id must be a safe non-baseline identifier")
    if args.parent != "baseline" and args.parent not in state["candidates"]:
        raise ValueError(f"unknown parent candidate: {args.parent}")
    root = candidate_dir(run, args.id)
    workspace = root / "source"
    metadata = {"id": args.id, "parent": args.parent, "mechanism": args.mechanism,
                "description": args.description, "workspace": str(workspace),
                "status": "prepared"}
    if workspace.exists():
        prior = state["candidates"].get(args.id)
        if prior and all(prior.get(key) == metadata.get(key)
                         for key in ["parent", "mechanism", "description", "workspace"]):
            return {"status": "cached", "candidate": args.id, "workspace": display(workspace)}
        raise ValueError(f"candidate workspace already exists: {workspace}")
    parent = baseline if args.parent == "baseline" else Path(state["candidates"][args.parent]["workspace"])
    ignore = shutil.ignore_patterns(*plan["source"].get("copy_ignore", []))
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(parent, workspace, ignore=ignore)
    write(root / "candidate.json", metadata)
    state["candidates"][args.id] = metadata
    save_state(run, state)
    refresh_brief(plan, run, state)
    return {"status": "prepared", "candidate": args.id, "parent": args.parent,
            "workspace": display(workspace)}


def prepare_batch(plan: dict, run: Path, variables: dict[str, str], specs: list[str]) -> dict:
    if not specs or len(specs) > 3:
        raise ValueError("prepare-batch requires one to three --candidate specs")
    rows = []
    for spec in specs:
        fields = spec.split(":", 3)
        if len(fields) not in {3, 4}:
            raise ValueError("candidate spec is ID:PARENT:MECHANISM[:DESCRIPTION]")
        candidate_id, parent, mechanism = fields[:3]
        description = fields[3] if len(fields) == 4 else mechanism
        rows.append(prepare(plan, run, variables, SimpleNamespace(
            id=candidate_id, parent=parent, mechanism=mechanism, description=description)))
    return {"status": "prepared", "root": display(run / "candidates"),
            "candidates": [row["candidate"] for row in rows]}


def readable(path: Path) -> list[str] | None:
    try:
        return path.read_text().splitlines(keepends=True)
    except (UnicodeDecodeError, OSError):
        return None


def make_patch(parent: Path, workspace: Path, patterns: list[str], output: Path) -> str:
    left = {str(path.relative_to(parent)): path for path in source_files(parent, patterns)}
    right = {str(path.relative_to(workspace)): path for path in source_files(workspace, patterns)}
    chunks = []
    for name in sorted(set(left) | set(right)):
        before = readable(left[name]) if name in left else []
        after = readable(right[name]) if name in right else []
        if before is None or after is None or before == after:
            continue
        chunks.extend(difflib.unified_diff(before, after,
                      fromfile=f"parent/{name}", tofile=f"candidate/{name}", n=3))
    text = "".join(chunks)
    output.write_text(text)
    return text


def item(plan: dict, workspace: Path, candidate_id: str, patch: Path | None,
         description=None, mechanism=None, build=True) -> dict:
    variables = {"repo": str(REPO), "run_dir": str(context(plan)[0]),
                 "run_id": plan["run_id"], "workspace": str(workspace), "id": candidate_id}
    execution = plan["execution"]
    result = {"id": candidate_id,
              "command": [expand(value, variables) for value in execution["command"]],
              "cwd": expand(execution.get("cwd", "{workspace}"), variables),
              "fingerprint_inputs": [str(path) for path in source_files(
                  workspace, plan["source"]["fingerprint_globs"])],
              "description": description, "mechanism": mechanism,
              "patch": str(patch) if patch else None}
    if build and execution.get("build"):
        result["build"] = [expand(value, variables) for value in execution["build"]]
        result["build_cwd"] = expand(execution.get("build_cwd", "{workspace}"), variables)
        if execution.get("build_timeout_s") is not None:
            result["build_timeout_s"] = execution["build_timeout_s"]
    if execution.get("timeout_s") is not None:
        result["timeout_s"] = execution["timeout_s"]
    if execution.get("env"):
        result["env"] = {key: expand(value, variables) for key, value in execution["env"].items()}
    return result


def campaign_plan(plan: dict, run: Path, baseline: Path, state: dict,
                  ids: list[str], phase: str, batch: Path) -> dict:
    candidates = []
    for candidate_id in ids:
        meta = state["candidates"].get(candidate_id)
        if not meta:
            raise ValueError(f"unknown candidate: {candidate_id}")
        workspace = Path(meta["workspace"])
        parent = baseline if meta["parent"] == "baseline" else Path(state["candidates"][meta["parent"]]["workspace"])
        patch = candidate_dir(run, candidate_id) / "change.patch"
        if not make_patch(parent, workspace, plan["source"]["fingerprint_globs"], patch):
            raise ValueError(f"candidate {candidate_id} has no source change relative to {meta['parent']}")
        candidates.append(item(plan, workspace, candidate_id, patch,
                               meta["description"], meta["mechanism"], build=True))
    campaign_phase = "final-only" if phase == "confirm" else phase
    result = {"version": 1, "phase": campaign_phase,
              "run_id": f"{plan['run_id']}-{batch.name}", "run_dir": str(batch),
              "env": plan.get("env", {}), "fingerprint_inputs": [str((run / "experiment-plan.json"))],
              "metric": plan["metric"], "oracle": plan.get("oracle"),
              "sampling": plan["sampling"], "timeout_s": plan.get("timeout_s"),
              "baseline": item(plan, baseline, "baseline", None, build=plan.get("build_baseline", False)),
              "candidates": candidates}
    if campaign_phase == "final-only":
        screening = []
        for candidate_id in ids:
            prior = state["candidates"][candidate_id].get("result")
            if not prior:
                raise ValueError(f"candidate {candidate_id} has no screening result")
            screening.append(prior)
        result["screening"] = screening
    if plan.get("attribution"):
        result["attribution"] = plan["attribution"]
    return {key: value for key, value in result.items() if value is not None}


def compact_candidate(row: dict) -> dict:
    keep = lambda value: {key: value[key] for key in ["median", "mad", "min", "max"] if key in value}
    return {"id": row["id"], "description": row.get("description"),
            "mechanism": row.get("mechanism"),
            "correctness": row["correctness"],
            "pairs": row["pairs"], "stop": row["stop"],
            "metric": keep(row["metric"]),
            "baseline_metric": keep(row["baseline_metric"]),
            "improvement": row.get("improvement"),
            "wall_improvement": row.get("wall_improvement")}


def compact_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    return 0 if value == 0 else float(f"{value:.6g}")


def profile_variables(plan: dict, run: Path, candidate_id: str, workspace: Path) -> dict[str, str]:
    profile_dir = candidate_dir(run, candidate_id) / "profile"
    variables = {"repo": str(REPO), "run_id": plan["run_id"], "run_dir": str(run),
                 "id": candidate_id, "workspace": str(workspace),
                 "profile_dir": str(profile_dir),
                 "profile_raw": str(run / "raw/iteration" / candidate_id)}
    profile = plan["iteration_profile"]
    variables["baseline_profile"] = str(resolve(profile["baseline_profile"], variables))
    variables["profile_output"] = str(resolve(profile["profile_output"], variables))
    variables["diff_output"] = str(profile_dir / "profile-diff.json")
    return variables


def profile_signature(plan: dict, workspace: Path, variables: dict[str, str]) -> str:
    dependencies = []
    for stage in plan["iteration_profile"]["stages"]:
        for value in stage.get("fingerprint_inputs", []):
            path = resolve(value, variables)
            dependencies.append({"path": str(path),
                                 "sha256": sha256(path) if path.is_file() else None})
    material = {
        "contract": plan["iteration_profile"],
        "source": source_state(workspace, plan["source"]["fingerprint_globs"]),
        "baseline_profile_sha256": sha256(Path(variables["baseline_profile"])),
        "dependencies": dependencies,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()


def metric_value(item: dict, metric: str):
    value = item.get(metric)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = (item.get("metrics") or {}).get(metric)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def target_delta(plan: dict, baseline: dict, variant: dict) -> dict | None:
    attribution = plan.get("attribution") or {}
    if not attribution:
        return None
    metric = attribution.get("metric", "self_time_s")

    def total(profile: dict, pattern: str):
        expression = re.compile(pattern)
        values = [metric_value(item, metric) for item in profile.get("hotspots", [])
                  if expression.search(item.get("name", ""))]
        values = [value for value in values if value is not None]
        return sum(values) if values else None

    before = total(baseline, attribution["baseline_target_regex"])
    after = total(variant, attribution["variant_target_regex"])
    change = ((after / before - 1) * 100
              if isinstance(before, (int, float)) and before and isinstance(after, (int, float))
              else None)
    return {"metric": metric, "base": compact_number(before), "opt": compact_number(after),
            "pct": compact_number(change)}


def compact_movement(diff: dict, direction: str) -> dict | None:
    preferred = [item for item in diff.get("metric_deltas", [])
                 if item.get("metric") == "self_time_s"]
    groups = preferred or diff.get("metric_deltas", [])
    rows = [(group.get("metric"), row) for group in groups
            for row in group.get(direction, [])]
    if not rows:
        return None
    metric, row = max(rows, key=lambda pair: abs(pair[1].get("wall_impact_pct")
                                                  or pair[1].get("delta") or 0))
    return {key: value for key, value in {
        "name": row.get("name"), "file": row.get("file"), "line": row.get("line"),
        "metric": metric, "pct": compact_number(row.get("change_pct")),
        "wall_pct": compact_number(row.get("wall_impact_pct")),
    }.items() if value is not None}


def compact_ncu(profile: dict) -> dict | None:
    diagnostics = profile.get("kernel_diagnostics") or {}
    kernels = diagnostics.get("kernels") or []
    if not kernels:
        return None
    kernel = kernels[0]
    def value(section, name):
        return ((kernel.get(section) or {}).get(name) or {}).get("value")
    occupancy = kernel.get("occupancy") or {}
    def prune(value):
        if isinstance(value, dict):
            return {key: prune(item) for key, item in value.items() if item is not None}
        if isinstance(value, list):
            return [prune(item) for item in value if item is not None]
        return value
    timing = ((diagnostics.get("collection") or {}).get("timing") or {})
    requested = ((diagnostics.get("collection") or {}).get("requested") or {})
    source = ((diagnostics.get("source_hotspots") or [None])[0]) or {}
    return prune({
        "kernel": kernel.get("name"), "section": requested.get("section"),
        "capture_s": timing.get("capture_elapsed_s"),
        "export_s": timing.get("export_elapsed_s"),
        "total_s": timing.get("total_elapsed_s"),
        "metric_count": (diagnostics.get("context") or {}).get("raw_metric_rows"),
        "launch": {"grid": value("launch", "grid_threads"),
                   "block": value("launch", "block_threads"),
                   "registers_per_thread": value("launch", "registers_per_thread"),
                   "static_shared_bytes": value("launch", "static_shared_bytes"),
                   "dynamic_shared_bytes": value("launch", "dynamic_shared_bytes"),
                   "waves_per_sm": value("launch", "waves_per_sm")},
        "occupancy": {"achieved_pct": value("occupancy", "achieved_pct"),
                      "theoretical_pct": value("occupancy", "theoretical"),
                      "limiters": [{"resource": item.get("resource"), "value": item.get("value")}
                                   for item in occupancy.get("limiters", [])]},
        "scheduler": {"active_warps": value("scheduler", "active_warps_per_scheduler"),
                      "eligible_warps": value("scheduler", "eligible_warps_per_scheduler"),
                      "issued_warps": value("scheduler", "issued_warps_per_scheduler"),
                      "issue_active_pct": value("scheduler", "issue_active_pct")},
        "stalls": [{"reason": item.get("reason"), "value": item.get("value")}
                   for item in (kernel.get("warp_stalls") or [])[:3]],
        "throughput_pct": {"sm": value("throughput", "sm_pct"),
                           "compute_memory": value("throughput", "compute_memory_pct"),
                           "dram": value("throughput", "dram_pct"),
                           "l1": value("throughput", "l1_pct"),
                           "l2": value("throughput", "l2_pct")},
        "memory": {"l1_hit_pct": value("memory", "l1_hit_pct"),
                   "l2_hit_pct": value("memory", "l2_hit_pct"),
                   "global_load_bytes_per_sector": value("memory", "global_load_bytes_per_sector"),
                   "global_store_bytes_per_sector": value("memory", "global_store_bytes_per_sector"),
                   "spill_requests_pct": value("memory", "local_spill_requests_pct"),
                   "shared_bank_conflicts": value("memory", "shared_bank_conflicts")},
        "instructions": {"executed": value("instructions", "executed"),
                         "branch_instruction_pct": value("instructions", "branch_instruction_pct"),
                         "threads_per_instruction": value("instructions", "threads_executed_per_instruction"),
                         "top_pipes": [{"pipe": item.get("pipe"), "value": item.get("value")}
                                       for item in (kernel.get("instructions") or {}).get("top_compute_pipes", [])]},
        "signals": [item.get("kind") for item in (kernel.get("signals") or [])[:3]],
        "rules": [{"name": item.get("name"), "estimated_speedup": item.get("estimated_speedup")}
                  for item in (diagnostics.get("rules") or [])[:3]],
        "source_hotspot": {key: source.get(key) for key in
                           ["address", "instruction", "samples", "not_issued_samples",
                            "dominant_not_issued_stall"]},
        "drilldowns": list((diagnostics.get("drilldowns") or {}).keys()),
    })


def nested(value: dict, *path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def ncu_delta(baseline: dict | None, variant: dict | None) -> list[dict]:
    """Return a stable, bounded counter delta vector for an agent decision."""
    if not baseline or not variant:
        return []
    metrics = [
        ("registers/thread", ("launch", "registers_per_thread")),
        ("occupancy%", ("occupancy", "achieved_pct")),
        ("eligible-warps", ("scheduler", "eligible_warps")),
        ("issue-active%", ("scheduler", "issue_active_pct")),
        ("SM%", ("throughput_pct", "sm")),
        ("DRAM%", ("throughput_pct", "dram")),
        ("L1%", ("throughput_pct", "l1")),
        ("L2%", ("throughput_pct", "l2")),
        ("L1-hit%", ("memory", "l1_hit_pct")),
        ("L2-hit%", ("memory", "l2_hit_pct")),
        ("global-load-B/sector", ("memory", "global_load_bytes_per_sector")),
        ("spill%", ("memory", "spill_requests_pct")),
        ("instructions", ("instructions", "executed")),
        ("branch%", ("instructions", "branch_instruction_pct")),
        ("threads/instruction", ("instructions", "threads_per_instruction")),
    ]
    changes = []
    for name, path in metrics:
        before, after = nested(baseline, *path), nested(variant, *path)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in [before, after]):
            continue
        pct = ((after / before - 1) * 100) if before else None
        changes.append({"metric": name, "base": compact_number(before),
                        "opt": compact_number(after), "pct": compact_number(pct)})
    baseline_stalls = {item.get("reason"): item.get("value")
                       for item in baseline.get("stalls", [])}
    for item in variant.get("stalls", [])[:3]:
        reason, after = item.get("reason"), item.get("value")
        before = baseline_stalls.get(reason)
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   for value in [before, after]):
            continue
        pct = ((after / before - 1) * 100) if before else None
        changes.append({"metric": f"stall:{reason}", "base": compact_number(before),
                        "opt": compact_number(after), "pct": compact_number(pct)})
    return changes


def kernel_profile_pattern(plan: dict, phase: str) -> str:
    config = plan.get("kernel_profile") or {}
    attribution = plan.get("attribution") or {}
    key = "baseline_kernel_regex" if phase == "baseline" else "variant_kernel_regex"
    fallback = "baseline_target_regex" if phase == "baseline" else "variant_target_regex"
    return config.get(key) or attribution.get(fallback)


def kernel_profile_locations(run: Path, candidate_id: str):
    if candidate_id == "baseline":
        return (run / "raw/ncu/baseline", run / "profile.ncu.baseline.json",
                run / "kernel-diagnostics.json", run / "ncu-drilldowns", run / "ncu-cache.json")
    if candidate_id == "final":
        root = run / "ncu-final"
        return (run / "raw/ncu/final", root / "profile.ncu.reprofile.json",
                root / "kernel-diagnostics.json", root / "drilldowns", root / "ncu-cache.json")
    root = candidate_dir(run, candidate_id) / "profile"
    return (run / "raw/ncu" / candidate_id, root / "profile.ncu.variant.json",
            root / "kernel-diagnostics.json", root / "ncu-drilldowns", root / "ncu-cache.json")


def kernel_evidence_fingerprint(plan: dict, run: Path, candidate_id: str,
                                workspace: Path) -> tuple[str, list[dict]]:
    """Identify whether the executable/device-code evidence changed across candidates.

    Explicit fingerprint inputs are preferred. Otherwise a workload executable inside the
    candidate workspace is used. Interpreted or launcher-based workloads conservatively fall
    back to the declared source fingerprint, which causes a replay instead of an unsafe reuse.
    """
    variables = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
                 "workspace": str(workspace), "id": candidate_id}
    config = plan.get("kernel_profile") or {}
    declared = config.get("fingerprint_inputs") or []
    states = [evidence_path_state(resolve(value, variables), workspace) for value in declared]
    missing = [item["path"] for item in states if item.get("state") == "missing"]
    if missing:
        raise ValueError("kernel_profile fingerprint input missing: " + ",".join(missing))
    if not states:
        execution = plan["execution"]
        command0 = Path(expand(execution["command"][0], variables))
        cwd = resolve(execution.get("cwd", "{workspace}"), variables)
        executable = command0 if command0.is_absolute() else cwd / command0
        try:
            local_executable = executable.resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            local_executable = False
        if local_executable and executable.is_file():
            states = [evidence_path_state(executable, workspace)]
        else:
            states = [{"path": "declared-source", "state": "source-tree",
                       "files": source_state(workspace, plan["source"]["fingerprint_globs"])}]
    material = {"inputs": states, "kernel": kernel_profile_pattern(plan, "variant"),
                "section": config.get("section", "all")}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return digest, states


def kernel_profile_signature(plan: dict, workspace: Path, timeline: Path,
                             phase: str, candidate_id: str) -> str:
    material = {
        "config": plan.get("kernel_profile"), "phase": phase, "candidate": candidate_id,
        "command": plan.get("execution", {}).get("command"),
        "source": source_state(workspace, plan["source"]["fingerprint_globs"]),
        "timeline": sha256(timeline),
        "capture_script": sha256(NCU_CAPTURE), "reduce_script": sha256(NCU_REDUCE),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()


def run_kernel_profile(plan: dict, run: Path, candidate_id: str, workspace: Path,
                       timeline_profile: Path, phase: str, force: bool,
                       reason: str | None = None) -> dict:
    """Capture, reduce, validate, and cache one NCU kernel without agent-authored plans."""
    config = plan.get("kernel_profile") or {}
    if not config.get("enabled"):
        return {"status": "disabled"}
    raw, output, diagnostics, drilldowns, cache_path = kernel_profile_locations(run, candidate_id)
    signature = kernel_profile_signature(plan, workspace, timeline_profile, phase, candidate_id)
    evidence_fingerprint, evidence_inputs = kernel_evidence_fingerprint(
        plan, run, candidate_id, workspace)
    cached = load(cache_path, {})
    if (not force and cached.get("signature") == signature and output.is_file() and
            diagnostics.is_file() and cached.get("profile_sha256") == sha256(output) and
            cached.get("diagnostics_sha256") == sha256(diagnostics)):
        capsule = compact_ncu(load(output, {})) or {}
        return {"status": "complete", "cache": "hit", "disposition": "cache-hit",
                "reason": "candidate-capture-cache-valid", "spent_s": 0,
                "evidence_fingerprint": evidence_fingerprint,
                "evidence_inputs": evidence_inputs, "artifact": display(output), **capsule}

    raw.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    drilldowns.mkdir(parents=True, exist_ok=True)
    variables = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
                 "workspace": str(workspace), "id": candidate_id}
    execution = plan["execution"]
    command = [expand(value, variables) for value in execution["command"]]
    cwd = resolve(execution.get("cwd", "{workspace}"), variables)
    capture = [sys.executable, str(NCU_CAPTURE), "--output-dir", str(raw),
               "--kernel-name", kernel_profile_pattern(plan, phase),
               "--launch-count", str(config.get("launch_count", 1)),
               "--section", config.get("section", "all"),
               "--timeout-s", str(config.get("timeout_s", 900)), "--cwd", str(cwd)]
    if config.get("exact_kernel"):
        capture.append("--exact-kernel")
    if config.get("executable"):
        capture += ["--ncu", expand(config["executable"], variables)]
    if config.get("tool_version"):
        capture += ["--tool-version", str(config["tool_version"])]
    capture += ["--", *command]
    env = (os.environ.copy() | expanded_env(plan.get("env", {}), variables) |
           expanded_env(execution.get("env", {}), variables) |
           expanded_env(config.get("env", {}), variables))
    capture_proc = subprocess.run(capture, cwd=REPO, env=env, capture_output=True, text=True,
                                  timeout=config.get("timeout_s", 900) + 60)
    (raw / "controller-capture.stdout.txt").write_text(capture_proc.stdout)
    (raw / "controller-capture.stderr.txt").write_text(capture_proc.stderr)
    if capture_proc.returncode:
        raise RuntimeError(f"NCU capture failed for {candidate_id}; see {display(raw)}")

    reduce = [sys.executable, str(NCU_REDUCE), "--csv", str(raw / "metrics.csv"),
              "--capture-metadata", str(raw / "capture.json"),
              "--details-csv", str(raw / "details.csv"), "--source-csv", str(raw / "source.csv"),
              "--timeline-profile", str(timeline_profile), "--run-id", plan["run_id"],
              "--phase", phase, "--output", str(output),
              "--diagnostics-output", str(diagnostics), "--drilldown-dir", str(drilldowns)]
    if candidate_id not in {"baseline", "final"}:
        reduce += ["--variant", candidate_id]
    reduce_proc = subprocess.run(reduce, cwd=REPO, capture_output=True, text=True)
    (raw / "controller-reduce.stdout.txt").write_text(reduce_proc.stdout)
    (raw / "controller-reduce.stderr.txt").write_text(reduce_proc.stderr)
    if reduce_proc.returncode or not output.is_file() or not diagnostics.is_file():
        raise RuntimeError(f"NCU reduction failed for {candidate_id}; see {display(raw)}")
    for schema, document in [("profile.schema.json", output),
                             ("ncu-diagnostics.schema.json", diagnostics)]:
        validate = subprocess.run(
            [sys.executable, str(REPO / "shared/scripts/validate.py"),
             str(REPO / "shared/schema" / schema), str(document)],
            cwd=REPO, capture_output=True, text=True)
        (raw / f"validate-{schema}.stdout.txt").write_text(validate.stdout)
        (raw / f"validate-{schema}.stderr.txt").write_text(validate.stderr)
        if validate.returncode:
            raise RuntimeError(f"invalid NCU artifact for {candidate_id}; see {display(raw)}")
    write(cache_path, {"protocol": "profileweaver.ncu-cache/v1", "signature": signature,
                       "evidence_fingerprint": evidence_fingerprint,
                       "profile_sha256": sha256(output),
                       "diagnostics_sha256": sha256(diagnostics)})
    capsule = compact_ncu(load(output, {})) or {}
    return {"status": "complete", "cache": "miss", "disposition": "captured",
            "reason": reason or ("baseline" if candidate_id == "baseline" else
                                  "accepted-winner-fresh-replay" if candidate_id == "final" else
                                  "kernel-evidence-fingerprint-changed"),
            "spent_s": capsule.get("total_s"),
            "evidence_fingerprint": evidence_fingerprint,
            "evidence_inputs": evidence_inputs, "artifact": display(output), **capsule}


def candidate_kernel_profile(plan: dict, run: Path, candidate_id: str, workspace: Path,
                             timeline_profile: Path, parent_id: str,
                             parent_profile: dict | None, force: bool = False) -> dict:
    """Capture changed kernel evidence or explicitly reuse identical parent evidence."""
    fingerprint, inputs = kernel_evidence_fingerprint(plan, run, candidate_id, workspace)
    parent_fingerprint = (parent_profile or {}).get("evidence_fingerprint")
    if not force and parent_profile and parent_fingerprint == fingerprint:
        capsule = {key: value for key, value in parent_profile.items()
                   if key not in {"delta", "delta_baseline", "delta_parent", "cache",
                                  "disposition", "reason", "reused_from", "artifact",
                                  "source_artifact", "spent_s", "evidence_inputs"}}
        reuse_path = candidate_dir(run, candidate_id) / "profile/ncu-reuse.json"
        source_artifact = (parent_profile.get("source_artifact") or
                           parent_profile.get("artifact"))
        capsule.update({
            "status": "complete", "cache": "reused", "disposition": "reused",
            "reason": "kernel-evidence-fingerprint-unchanged", "reused_from": parent_id,
            "spent_s": 0, "evidence_fingerprint": fingerprint,
            "evidence_inputs": inputs, "source_artifact": source_artifact,
            "artifact": display(reuse_path),
        })
        write(reuse_path, {"protocol": "profileweaver.ncu-reuse/v1",
                           "candidate": candidate_id, "reused_from": parent_id,
                           "reason": capsule["reason"], "evidence_fingerprint": fingerprint,
                           "source_artifact": source_artifact, "capsule": capsule})
        return capsule
    return run_kernel_profile(
        plan, run, candidate_id, workspace, timeline_profile, "variant", force=force,
        reason=("agent-forced-replay" if force else
                "kernel-evidence-fingerprint-changed" if parent_profile else
                "parent-kernel-evidence-unavailable"))


def profile_capsule(plan: dict, candidate_id: str, baseline: dict, variant: dict,
                    diff: dict, sources: dict, artifact: Path, cache: str,
                    kernel_profile: dict | None = None,
                    baseline_kernel_profile: dict | None = None,
                    parent_kernel_profile: dict | None = None) -> dict:
    remaining = diff.get("remaining_leverage") or {}
    source_target = next((item for item in sources.get("targets", [])
                          if item.get("hotspot") == remaining.get("name")), None)
    source = (source_target or {}).get("source") or {}
    source_slice = (source_target or {}).get("slice") or {}
    resolution = (source_target or {}).get("resolution") or {}
    next_target = {key: value for key, value in {
        "name": remaining.get("name"), "file": source.get("path") or remaining.get("file"),
        "line": source.get("match_line") or remaining.get("line"),
        "lines": ([source.get("start_line"), source.get("end_line")]
                  if source.get("start_line") and source.get("end_line") else None),
        "slice": source_slice.get("path"), "confidence": resolution.get("confidence"),
        "wall_pct": remaining.get("pct_of_variant_wall"),
    }.items() if value is not None}
    ncu = kernel_profile or compact_ncu(variant)
    if ncu and baseline_kernel_profile:
        ncu = dict(ncu)
        ncu["delta_baseline"] = ncu_delta(baseline_kernel_profile, ncu)
        ncu["delta"] = ncu["delta_baseline"]  # Backward-compatible artifact field.
        ncu["delta_parent"] = ncu_delta(parent_kernel_profile or baseline_kernel_profile, ncu)
    return {key: value for key, value in {
        "id": candidate_id, "status": "complete", "cache": cache,
        "wall_pct": ((diff.get("headline") or {}).get("wall_s") or {}).get("change_pct"),
        "gpu_pct": ((diff.get("headline") or {}).get("gpu_busy_s") or {}).get("change_pct"),
        "target": target_delta(plan, baseline, variant),
        "gain": compact_movement(diff, "decreases"),
        "regression": compact_movement(diff, "increases"),
        "ncu": ncu,
        "next": next_target or None,
        "artifact": display(artifact),
    }.items() if value is not None}


def cached_profile(cache_path: Path, signature: str, profile_path: Path,
                   diff_path: Path, source_path: Path) -> dict | None:
    cached = load(cache_path, {})
    if (cached.get("signature") != signature or not profile_path.is_file() or
            not diff_path.is_file() or not source_path.is_file()):
        return None
    hashes = cached.get("artifacts") or {}
    if (hashes.get("profile") != sha256(profile_path) or
            hashes.get("diff") != sha256(diff_path) or
            hashes.get("sources") != sha256(source_path)):
        return None
    capsule = cached.get("capsule")
    if not isinstance(capsule, dict):
        return None
    return capsule | {"cache": "hit"}


def terminal_ncu(value: dict | None) -> dict | None:
    """Keep command stdout useful and small; the full vector remains in state/artifacts."""
    if not value or value.get("status") not in {None, "complete"}:
        return value
    stalls = value.get("stalls") or []
    top_stall = stalls[0] if stalls else None
    def delta_summary(rows: list[dict] | None) -> dict | None:
        values = [item for item in (rows or []) if item.get("pct") is not None]
        values.sort(key=lambda item: abs(item.get("pct") or 0), reverse=True)
        selected = values[:5]
        return {item["metric"]: item.get("pct") for item in selected} or None
    disposition = value.get("disposition") or value.get("cache")
    reason_codes = {
        "kernel-evidence-fingerprint-unchanged": "kernel-unchanged",
        "kernel-evidence-fingerprint-changed": "kernel-changed",
        "candidate-capture-cache-valid": "capture-cache-valid",
        "candidate-profile-cache-valid": "profile-cache-valid",
        "agent-forced-replay": "forced-replay",
        "accepted-winner-fresh-replay": "winner-reprofile",
        "parent-kernel-evidence-unavailable": "parent-evidence-missing",
    }
    if disposition == "reused":
        return {key: item for key, item in {
            "profile": "reused", "reason": "kernel-unchanged", "spent_s": 0,
            "from": value.get("reused_from"), "delta_base": "unchanged",
            "delta_parent": "unchanged", "artifact": value.get("artifact"),
        }.items() if item is not None}
    if disposition == "cache-hit" and value.get("previous_disposition"):
        return {key: item for key, item in {
            "profile": "cache-hit", "reason": "profile-cache-valid", "spent_s": 0,
            "evidence_s": value.get("total_s"),
            "delta_base_pct": delta_summary(value.get("delta_baseline") or value.get("delta")),
            "delta_parent_pct": delta_summary(value.get("delta_parent")),
            "artifact": value.get("artifact"),
        }.items() if item is not None}
    return {key: item for key, item in {
        "profile": disposition,
        "reason": reason_codes.get(value.get("reason"), value.get("reason")),
        "spent_s": value.get("spent_s"),
        "evidence_s": value.get("total_s"), "from": value.get("reused_from"),
        "metrics": value.get("metric_count"),
        "regs": nested(value, "launch", "registers_per_thread"),
        "occ_pct": nested(value, "occupancy", "achieved_pct"),
        "eligible": nested(value, "scheduler", "eligible_warps"),
        "issue_pct": nested(value, "scheduler", "issue_active_pct"),
        "stall": (f"{top_stall.get('reason')}:{top_stall.get('value')}" if top_stall else None),
        "load_B_sector": nested(value, "memory", "global_load_bytes_per_sector"),
        "delta_base_pct": delta_summary(value.get("delta_baseline") or value.get("delta")),
        "delta_parent_pct": delta_summary(value.get("delta_parent")),
        "artifact": value.get("artifact"),
    }.items() if item is not None}


def terminal_profile(value: dict, confirmation: bool = False) -> dict:
    if confirmation:
        return {key: item for key, item in {
            "id": value.get("id"), "status": value.get("status"),
            "cache": value.get("cache"), "wall_pct": value.get("wall_pct"),
            "target_pct": (value.get("target") or {}).get("pct"),
        }.items() if item is not None}
    remaining = value.get("next") or {}
    next_target = {key: item for key, item in {
        "name": remaining.get("name"), "slice": remaining.get("slice"),
        "wall_pct": remaining.get("wall_pct"),
    }.items() if item is not None}
    return {key: item for key, item in {
        "id": value.get("id"), "status": value.get("status"), "cache": value.get("cache"),
        "wall_pct": value.get("wall_pct"), "gpu_pct": value.get("gpu_pct"),
        "target": value.get("target"), "gain": value.get("gain"),
        "regression": value.get("regression"), "next": next_target or None,
        "ncu": terminal_ncu(value.get("ncu")), "artifact": value.get("artifact"),
    }.items() if item is not None}


def run_iteration_profile(plan: dict, run: Path, candidate_id: str, row: dict,
                          parent_id: str, baseline_kernel_profile: dict | None,
                          parent_kernel_profile: dict | None,
                          force_ncu: bool = False) -> dict:
    if not (row.get("correctness") or {}).get("passed"):
        skipped = {"id": candidate_id, "status": "skipped-incorrect"}
        if (plan.get("kernel_profile") or {}).get("enabled"):
            skipped["ncu"] = {"status": "skipped", "disposition": "skipped",
                              "reason": "correctness-failed", "spent_s": 0}
        return skipped
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
        ncu_current = (not (plan.get("kernel_profile") or {}).get("enabled") or
                       (cached_ncu.get("evidence_fingerprint") and
                        cached_ncu.get("delta_parent") is not None))
        if ncu_current and not force_ncu:
            if cached_ncu:
                cached["ncu"] = dict(cached_ncu) | {
                    "disposition": "cache-hit", "reason": "candidate-profile-cache-valid",
                    "spent_s": 0, "previous_disposition": cached_ncu.get("disposition")}
            return cached
        baseline = load(Path(variables["baseline_profile"]), {})
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

    diff_argv = [sys.executable, str(REPO / "shared/scripts/diff_profiles.py"),
                 "--baseline", variables["baseline_profile"], "--variant", str(profile_path),
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

    baseline = load(Path(variables["baseline_profile"]), {})
    variant = load(profile_path, {})
    sources = load(source_path, {})
    kernel = (candidate_kernel_profile(plan, run, candidate_id, workspace, profile_path,
                                       parent_id, parent_kernel_profile, force=force_ncu)
              if (plan.get("kernel_profile") or {}).get("enabled") else None)
    capsule = profile_capsule(plan, candidate_id, baseline, variant, diff, sources, diff_path,
                              "miss", kernel, baseline_kernel_profile,
                              parent_kernel_profile)
    write(cache_path, {"protocol": "profileweaver.iteration-profile-cache/v1",
                       "signature": signature, "capsule": capsule,
                       "artifacts": {"profile": sha256(profile_path), "diff": sha256(diff_path),
                                     "sources": sha256(source_path)}})
    return capsule


def run_campaign(plan: dict, plan_path: Path, run: Path, state: dict,
                 baseline: Path, ids: list[str], phase: str,
                 diagnose_ids: list[str] | None = None) -> dict:
    if not ids or len(ids) > 3:
        raise ValueError("evaluate/confirm requires one to three candidate ids")
    force_ncu = set(diagnose_ids or [])
    unknown_diagnose = force_ncu - set(ids)
    if unknown_diagnose:
        raise ValueError(f"--ncu candidates are not in this batch: {','.join(sorted(unknown_diagnose))}")
    if force_ncu and not (plan.get("kernel_profile") or {}).get("enabled"):
        raise ValueError("--ncu requires kernel_profile.enabled=true in the experiment plan")
    sequence = len(state["batches"]) + 1
    name = (f"screen-{sequence:02d}" if phase == "screen" else f"final-{sequence:02d}")
    batch = run / "batches" / name
    batch.mkdir(parents=True, exist_ok=True)
    generated = campaign_plan(plan, run, baseline, state, ids, phase, batch)
    generated_path = batch / "campaign-plan.json"
    write(generated_path, generated)
    raw = run / "raw/experiment"
    raw.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, str(REPO / "shared/scripts/campaign.py"), str(generated_path)],
                          cwd=REPO, capture_output=True, text=True)
    (raw / f"{name}.stdout.txt").write_text(proc.stdout)
    (raw / f"{name}.stderr.txt").write_text(proc.stderr)
    result = load(batch / "campaign-result.json", {})
    if proc.returncode or result.get("status") != "complete":
        raise RuntimeError(f"{phase} campaign failed; see {raw / f'{name}.stderr.txt'}")
    rows = [compact_candidate(row) for row in result["candidates"]]
    accepted = phase != "confirm" or all(result["final"].get("gates", {}).values())
    by_id = {row["id"]: row for row in rows}
    for candidate_id, row in by_id.items():
        state["candidates"][candidate_id]["result"] = row
        state["candidates"][candidate_id]["status"] = (
            "screened" if phase == "screen" or not accepted else "confirmed")
    profiles = []
    baseline_kernel = (state.get("kernel_profiles") or {}).get("baseline")
    for candidate_id in ids:
        parent_id = state["candidates"][candidate_id]["parent"]
        parent_kernel = (baseline_kernel if parent_id == "baseline" else
                         ((state["candidates"].get(parent_id) or {}).get("profile") or {}).get("ncu"))
        capsule = run_iteration_profile(
            plan, run, candidate_id, by_id[candidate_id], parent_id,
            baseline_kernel, parent_kernel, force_ncu=candidate_id in force_ncu)
        state["candidates"][candidate_id]["profile"] = capsule
        profiles.append(capsule)
    winner = result["final"]["winner"]
    point = result["final"]["baseline_vs_winner"]["point"]
    winner_profile = next((item for item in profiles if item["id"] == winner), None)
    if winner_profile and winner_profile.get("status") == "complete":
        state["profile_frontier"] = {
            "candidate": winner, "target": winner_profile.get("target"),
            "next": winner_profile.get("next"), "artifact": winner_profile.get("artifact")}
    state["batches"].append({"id": name, "phase": phase, "candidates": ids,
                             "winner": winner, "improvement": point,
                             "profiles": [{"id": item["id"], "status": item["status"],
                                           "artifact": item.get("artifact")} for item in profiles],
                             "artifact": str(batch / "campaign-result.json")})
    if not state.get("best") or point > state["best"]["improvement"]:
        state["best"] = {"id": winner, "improvement": point,
                         "workspace": state["candidates"][winner]["workspace"]}
    if phase == "confirm":
        shutil.copy2(generated_path, run / "campaign-plan.json")
        shutil.copy2(batch / "campaign-result.json", run / "campaign-result.json")
        shutil.copy2(batch / "campaign-brief.txt", run / "campaign-brief.txt")
        state["last_confirmation"] = {"winner": winner, "accepted": accepted,
                                      "gates": result["final"].get("gates", {})}
        if accepted:
            state["confirmed"] = {"id": winner, "improvement": point,
                                  "workspace": state["candidates"][winner]["workspace"]}
            # The finalizer reads experiment-state.json. Persist confirmation before invoking it.
            save_state(run, state)
            run_final_stages(plan, run, state, result)
    save_state(run, state)
    refresh_brief(plan, run, state)
    return {"status": "complete" if accepted else "inconclusive", "phase": phase,
            "accepted": accepted, "winner": winner, "improvement": point,
            "pairs": result["final"]["pairs"],
            "profiles": [terminal_profile(item, confirmation=phase == "confirm")
                         for item in profiles], "batch": name,
            **({"final_ncu": terminal_ncu((state.get("kernel_profiles") or {}).get("final"))}
               if accepted and (state.get("kernel_profiles") or {}).get("final") else {})}


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
        env = os.environ.copy() | {key: expand(value, variables)
                                   for key, value in plan.get("env", {}).items()}
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                              timeout=stage.get("timeout_s"))
        (raw / f"{stage['id']}.stdout.txt").write_bytes(proc.stdout)
        (raw / f"{stage['id']}.stderr.txt").write_bytes(proc.stderr)
        missing = [str(resolve(value, variables)) for value in stage.get("produces", [])
                   if not resolve(value, variables).exists()]
        if proc.returncode or missing:
            raise RuntimeError(f"final stage {stage['id']} failed; see {raw}")
    kernel_config = plan.get("kernel_profile") or {}
    if kernel_config.get("enabled"):
        configured_timeline = kernel_config.get("final_timeline_profile",
                                                "{run_dir}/profile.reprofile.json")
        timeline = resolve(configured_timeline, variables)
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


def refresh_brief(plan: dict, run: Path, state: dict) -> None:
    candidates = list(state.get("candidates", {}).values())
    measured = [item for item in candidates if item.get("result")]
    ranked = sorted(measured, key=lambda item: (item["result"].get("improvement") or {}).get("point", 0),
                    reverse=True)
    best = state.get("best")
    top = ",".join(f"{item['id']}={(item['result'].get('improvement') or {}).get('point', 0):.3f}x"
                   for item in ranked[:3]) or "none"
    action = ("prepare up to three structurally distinct candidates" if not candidates else
              "batch unmeasured candidates" if any(not item.get("result") for item in candidates) else
              "prepare the next structural candidates or confirm the top two")
    deltas = []
    if state.get("batches"):
        for candidate_id in state["batches"][-1].get("candidates", [])[:3]:
            capsule = (state.get("candidates", {}).get(candidate_id) or {}).get("profile") or {}
            if capsule.get("status") != "complete":
                deltas.append(f"{candidate_id}:{capsule.get('status', 'missing')}")
                continue
            target = capsule.get("target") or {}
            remaining = capsule.get("next") or {}
            location = (f"@{remaining.get('file')}:{remaining.get('line')}"
                        if remaining.get("file") and remaining.get("line") else "")
            ncu = terminal_ncu(capsule.get("ncu")) or {}
            ncu_text = (f" ncu={ncu.get('profile')}/{ncu.get('spent_s')}s r={ncu.get('regs')} "
                        f"occ={ncu.get('occ_pct')} elig={ncu.get('eligible')} "
                        f"issue={ncu.get('issue_pct')} stall={ncu.get('stall')} "
                        f"ldB/sector={ncu.get('load_B_sector')} "
                        f"Δbase={ncu.get('delta_base_pct')} Δparent={ncu.get('delta_parent_pct')}"
                        if ncu else "")
            deltas.append(f"{candidate_id}:wall={capsule.get('wall_pct')}% "
                          f"target={target.get('pct')}% next={remaining.get('name')}{location}"
                          f"/{remaining.get('wall_pct')}%" + ncu_text)
    delta_line = "; ".join(deltas) or "none"
    text = (f"PW4 run={plan['run_id']}; ACTION: {action}.\n"
            f"STATE: candidates={len(candidates)} measured={len(measured)} batches={len(state.get('batches', []))}; "
            f"best={(best or {}).get('id')} improvement={(best or {}).get('improvement')}; top={top}.\n"
            f"DELTA: {delta_line}.\n"
            "OUTPUT: edit candidate source only; no heredoc/inline program, per-candidate plan, progress probe, JSON reader, or report script.\n"
            "VERIFY: use each cached iteration delta; confirm the best candidate, then freshly reprofile the final winner.\n")
    (run / "experiment-brief.txt").write_text(text)


def status(plan: dict, run: Path, variables: dict[str, str]) -> dict:
    state, _ = ensure_initialized(plan, run, variables)
    return {"status": "ready", "run_id": plan["run_id"],
            "candidates": len(state["candidates"]), "batches": len(state["batches"]),
            "best": ({key: state["best"][key] for key in ["id", "improvement"]}
                     if state.get("best") else None)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--id", required=True)
    prepare_parser.add_argument("--parent", default="baseline")
    prepare_parser.add_argument("--mechanism", required=True)
    prepare_parser.add_argument("--description", required=True)
    batch_parser = sub.add_parser("prepare-batch")
    batch_parser.add_argument("--candidate", action="append", required=True,
                              help="ID:PARENT:MECHANISM[:DESCRIPTION], repeat up to three times")
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("ids", nargs="+")
    evaluate_parser.add_argument("--ncu", action="append", default=[], metavar="ID",
                                 help="force a fresh full-set replay; automatic NCU/reuse is the default")
    confirm_parser = sub.add_parser("confirm")
    confirm_parser.add_argument("ids", nargs="+")
    confirm_parser.add_argument("--ncu", action="append", default=[], metavar="ID",
                                help="force a fresh candidate replay before final confirmation")
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        plan, run, variables = read_plan(args.plan)
        if args.command == "init":
            state, _ = ensure_initialized(plan, run, variables)
            result = {"status": "initialized", "run_id": plan["run_id"],
                      "contract": state["contract_sha256"][:12],
                      **({"ncu": terminal_ncu((state.get("kernel_profiles") or {}).get("baseline"))}
                         if (state.get("kernel_profiles") or {}).get("baseline") else {})}
        elif args.command == "prepare":
            result = prepare(plan, run, variables, args)
        elif args.command == "prepare-batch":
            result = prepare_batch(plan, run, variables, args.candidate)
        elif args.command in {"evaluate", "confirm"}:
            state, baseline = ensure_initialized(plan, run, variables)
            result = run_campaign(plan, args.plan.resolve(), run, state, baseline, args.ids,
                                  "screen" if args.command == "evaluate" else "confirm",
                                  diagnose_ids=args.ncu)
        else:
            result = status(plan, run, variables)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
