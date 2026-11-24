#!/usr/bin/env python3
"""Prepare inherited candidate trees and immutable campaign plans."""

from __future__ import annotations

from candidate_patch import readable, make_patch, numeric_only_patch

import difflib
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

from experiment_report import refresh_brief
from candidate_policy import seed_frontier
from experiment_state import context, ensure_initialized, save_state
from experiment_support import (REPO, candidate_dir, display, expand, source_files, write)
from adaptive_policy import enabled

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def prepare(plan: dict, run: Path, variables: dict[str, str], args) -> dict:
    state, baseline = ensure_initialized(plan, run, variables)
    if state.get("early_stop") and args.id not in state.get("candidates", {}):
        raise ValueError("flexible campaign has closed: " + state["early_stop"])
    seed_frontier(state, run)
    if not ID_PATTERN.match(args.id) or args.id == "baseline":
        raise ValueError("candidate id must be a safe non-baseline identifier")
    parent_id = args.parent
    if parent_id == "current":
        parent_id = ((state.get("confirmed") or state.get("best") or {}).get("id") or
                     "baseline")
        if enabled(plan) and not state.get("confirmed") and (state.get("best") or {}).get("improvement", 1) < 1:
            parent_id = "baseline"
    if parent_id != "baseline" and parent_id not in state["candidates"]:
        raise ValueError(f"unknown parent candidate: {args.parent}")
    # ProfileWeaver reports measured classifications; the agent owns candidate selection.
    # Candidate metadata is supplied by the agent, never generated as a permission gate.
    root = candidate_dir(run, args.id)
    workspace = root / "source"
    metadata = {"id": args.id, "parent": parent_id, "mechanism": args.mechanism,
                "description": args.description, "workspace": str(workspace),
                "status": "prepared"}
    from rewrite_evidence import proposal
    metadata.update(proposal(plan, run, state, parent_id, args))
    from selection_objective import candidate_selector
    metadata.update(candidate_selector(state, parent_id, getattr(args, "target_kernel", None)))
    if workspace.exists():
        prior = state["candidates"].get(args.id)
        if prior and all(prior.get(key) == metadata.get(key)
                         for key in ["parent", "mechanism", "description", "workspace", "hypothesis", "kernel_selector"]):
            return {"status": "cached", "candidate": args.id, "workspace": display(workspace)}
        raise ValueError(f"candidate workspace already exists: {workspace}")
    parent = baseline if parent_id == "baseline" else Path(state["candidates"][parent_id]["workspace"])
    ignore = shutil.ignore_patterns(*plan["source"].get("copy_ignore", []))
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(parent, workspace, ignore=ignore)
    write(root / "candidate.json", metadata)
    state["candidates"][args.id] = metadata
    save_state(run, state)
    refresh_brief(plan, run, state)
    return {"status": "prepared", "candidate": args.id, "parent": parent_id,
            "workspace": display(workspace)}


def prepare_batch(plan: dict, run: Path, variables: dict[str, str], specs: list[str], *, because=None, consider=None, target_kernel=None) -> dict:
    # A screen is deliberately one candidate.  Sibling candidates used to be
    # evaluated in one batch and then only the batch winner was promoted; any
    # useful mechanism in the other siblings was therefore lost instead of
    # becoming the parent of the next measured edit.  Keeping the old command
    # name preserves the one public entry point while making the state machine
    # sequential and feedback-driven.
    if len(specs) != 1:
        raise ValueError("prepare-batch requires exactly one --candidate; evaluate it before preparing the next")
    parsed = []
    for spec in specs:
        fields = spec.split(":", 3)
        if len(fields) not in {3, 4}:
            raise ValueError("candidate spec is ID:PARENT:MECHANISM[:DESCRIPTION]")
        candidate_id, parent, mechanism = fields[:3]
        description = fields[3] if len(fields) == 4 else mechanism
        parsed.append((candidate_id, parent, mechanism, description))
    state, _ = ensure_initialized(plan, run, variables)
    seed_frontier(state, run)
    # The brief contains no generated optimization hints. Never reject a candidate based on
    # an internal classification or because it combines linked source edits.
    rows = []
    for candidate_id, parent, mechanism, description in parsed:
        rows.append(prepare(plan, run, variables, SimpleNamespace(
            id=candidate_id, parent=parent, mechanism=mechanism, description=description, because=because, consider=consider, target_kernel=target_kernel)))
    return {"status": "prepared", "root": display(run / "candidates"),
            "candidates": [row["candidate"] for row in rows]}


def item(plan: dict, workspace: Path, candidate_id: str, patch: Path | None,
         description=None, mechanism=None, build=True) -> dict:
    variables = {"repo": str(REPO), "run_dir": str(context(plan)[0]),
                 "run_id": plan["run_id"], "workspace": str(workspace), "id": candidate_id}
    execution = plan["execution"]
    result = {"id": candidate_id,
              "workspace": str(workspace),
              "run_dir": variables["run_dir"],
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
        patch_text = make_patch(parent, workspace, plan["source"]["fingerprint_globs"], patch)
        if not patch_text:
            raise ValueError(f"candidate {candidate_id} has no source change relative to {meta['parent']}")
        change_class = "parameter-only" if numeric_only_patch(patch_text) else "structural-or-local"
        # Keep the classification for reporting, but never turn it into a candidate gate.
        meta["change_class"] = change_class
        candidates.append(item(plan, workspace, candidate_id, patch,
                               meta["description"], meta["mechanism"], build=True))
    campaign_phase = "final-only" if phase == "confirm" else phase
    result = {"version": 1, "phase": campaign_phase,
              "run_id": f"{plan['run_id']}-{batch.name}", "run_dir": str(batch),
              "env": plan.get("env", {}), "fingerprint_inputs": [str((run / "experiment-plan.json"))],
              "metric": plan["metric"], "oracle": plan.get("oracle"),
              "sampling": dict(plan["sampling"]), "timeout_s": plan.get("timeout_s"),
              "baseline": item(plan, baseline, "baseline", None, build=plan.get("build_baseline", False)),
              "candidates": candidates}
    if campaign_phase == "final-only":
        if result["sampling"].get("mode") == "adaptive":
            result["sampling"]["finalists"] = len(ids)
        screening = []
        for candidate_id in ids:
            prior = state["candidates"][candidate_id].get("result")
            if not prior:
                raise ValueError(f"candidate {candidate_id} has no screening result")
            screening.append(prior)
        result["screening"] = screening
    if plan.get("attribution"):
        result["attribution"] = plan["attribution"]
    if result["sampling"].get("mode") == "adaptive":
        if result["sampling"].get("measurement_policy") == "single-or-two-v3":
            from experiment_support import load
            result["baseline_reference"] = load(run / "baseline-timing.json", {}).get("sample")
        if phase == "screen":
            from adaptive_policy import warmup_for_profile
            from experiment_support import load
            count, reason = warmup_for_profile(result["sampling"], load(run / "profile.baseline.json", {}))
            result["sampling"]["screen_warmup"] = count
            if reason:
                result["sampling"]["warmup_decision"] = reason
        result["sampling"]["incumbent_speedup"] = (state.get("best") or {}).get("improvement", 1.0)
        from statistics import median
        prior_baselines = [(m.get("result", {}).get("baseline_metric") or {}).get("median")
                           for m in state.get("candidates", {}).values() if m.get("result")]
        prior_baselines = [v for v in prior_baselines if isinstance(v, (float, int)) and v > 0]
        if prior_baselines:
            result["sampling"]["screen_baseline_reference"] = median(prior_baselines)
        metric = plan.get("metric") or {}
        whole = metric.get("source") == "wall" or any(
            x in str(metric.get("name", "")).lower() for x in ("whole", "end-to-end", "total", "wall"))
        result["attribution"] = dict(result.get("attribution") or {}) | {
            "scope": "roi-wall" if whole else "roi-gpu-busy"}
    return {key: value for key, value in result.items() if value is not None}


def compact_candidate(row: dict) -> dict:
    def keep(value):
        return {key: value[key] for key in ["median", "mad", "min", "max"] if key in value}

    return {"id": row["id"], "description": row.get("description"),
            **{k: row[k] for k in ("runs", "benchmark_executions", "profiled_executions", "measurement_role", "baseline_reused", "confidence") if k in row},
            "mechanism": row.get("mechanism"),
            "correctness": row["correctness"],
            "pairs": row["pairs"], "stop": row["stop"],
            **({"ranking": row["ranking"]} if row.get("ranking") else {}),
            "metric": keep(row["metric"]),
            "baseline_metric": keep(row["baseline_metric"]),
            "improvement": row.get("improvement"),
            "wall_improvement": row.get("wall_improvement")}
