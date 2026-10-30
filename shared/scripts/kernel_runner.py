#!/usr/bin/env python3
"""Capture, reduce, validate, cache, and reuse one filtered NCU kernel."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from experiment_support import (REPO, candidate_dir, display, evidence_path_state, expand,
                                expanded_env, load, resolve, sha256, source_state, write)
from kernel_evidence import compact_ncu
from profile_policy import ncu_sections


CAPTURE = REPO / ".claude/skills/pw-nsight-compute/scripts/capture.py"
REDUCE = REPO / ".claude/skills/pw-nsight-compute/scripts/reduce_csv.py"


def kernel_pattern(plan: dict, phase: str) -> str:
    config, attribution = plan.get("kernel_profile") or {}, plan.get("attribution") or {}
    key = "baseline_kernel_regex" if phase == "baseline" else "variant_kernel_regex"
    fallback = "baseline_target_regex" if phase == "baseline" else "variant_target_regex"
    return config.get(key) or attribution.get(fallback)


def locations(run: Path, candidate_id: str):
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


def evidence_fingerprint(plan: dict, run: Path, candidate_id: str,
                         workspace: Path, diagnostic: dict | None = None) -> tuple[str, list[dict]]:
    values = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
              "workspace": str(workspace), "id": candidate_id}
    config = plan.get("kernel_profile") or {}
    states = [evidence_path_state(resolve(value, values), workspace)
              for value in config.get("fingerprint_inputs") or []]
    missing = [item["path"] for item in states if item.get("state") == "missing"]
    if missing:
        raise ValueError("kernel_profile fingerprint input missing: " + ",".join(missing))
    if not states:
        command0 = Path(expand(plan["execution"]["command"][0], values))
        cwd = resolve(plan["execution"].get("cwd", "{workspace}"), values)
        executable = command0 if command0.is_absolute() else cwd / command0
        try:
            local = executable.resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            local = False
        states = ([evidence_path_state(executable, workspace)] if local and executable.is_file()
                  else [{"path": "declared-source", "state": "source-tree",
                         "files": source_state(workspace, plan["source"]["fingerprint_globs"])}])
    material = {"inputs": states, "kernel": kernel_pattern(plan, "variant"),
                "sections": ncu_sections(config, "variant", diagnostic=diagnostic)}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return digest, states


def _signature(plan: dict, workspace: Path, timeline: Path, phase: str,
               candidate_id: str, force: bool, diagnostic: dict | None = None) -> str:
    material = {"config": plan.get("kernel_profile"), "phase": phase,
                "candidate": candidate_id, "force": force,
                "command": plan.get("execution", {}).get("command"),
                "source": source_state(workspace, plan["source"]["fingerprint_globs"]),
                "timeline": sha256(timeline),
                "sections": ncu_sections(plan.get("kernel_profile") or {}, phase, force,
                                         diagnostic),
                "ncu_scripts": source_state(CAPTURE.parent, ["*.py"])}
    return hashlib.sha256(json.dumps(material, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def run_kernel_profile(plan: dict, run: Path, candidate_id: str, workspace: Path,
                       timeline: Path, phase: str, force: bool,
                       reason: str | None = None, diagnostic: dict | None = None) -> dict:
    config = plan.get("kernel_profile") or {}
    if not config.get("enabled"):
        return {"status": "disabled"}
    raw, output, diagnostics, drilldowns, cache = locations(run, candidate_id)
    signature = _signature(plan, workspace, timeline, phase, candidate_id, force, diagnostic)
    fingerprint, inputs = evidence_fingerprint(plan, run, candidate_id, workspace, diagnostic)
    old = load(cache, {})
    if (not force and old.get("signature") == signature and output.is_file() and
            diagnostics.is_file() and old.get("profile_sha256") == sha256(output) and
            old.get("diagnostics_sha256") == sha256(diagnostics)):
        return {"status": "complete", "cache": "hit", "disposition": "cache-hit",
                "reason": "candidate-capture-cache-valid", "spent_s": 0,
                "evidence_fingerprint": fingerprint, "evidence_inputs": inputs,
                "artifact": display(output), **(compact_ncu(load(output, {})) or {})}

    raw.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    drilldowns.mkdir(parents=True, exist_ok=True)
    values = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
              "workspace": str(workspace), "id": candidate_id}
    execution = plan["execution"]
    command = [expand(value, values) for value in execution["command"]]
    cwd = resolve(execution.get("cwd", "{workspace}"), values)
    capture = [sys.executable, str(CAPTURE), "--output-dir", str(raw),
               "--kernel-name", kernel_pattern(plan, phase),
               "--launch-count", str(config.get("launch_count", 1)),
               "--timeout-s", str(config.get("timeout_s", 900)), "--cwd", str(cwd)]
    for section in ncu_sections(config, phase, force, diagnostic):
        capture += ["--section", section]
    if config.get("exact_kernel"):
        capture.append("--exact-kernel")
    if config.get("nvtx_include"):
        capture += ["--nvtx-include", expand(config["nvtx_include"], values)]
    if config.get("executable"):
        capture += ["--ncu", expand(config["executable"], values)]
    if config.get("tool_version"):
        capture += ["--tool-version", str(config["tool_version"])]
    env = (os.environ.copy() | expanded_env(plan.get("env", {}), values) |
           expanded_env(execution.get("env", {}), values) |
           expanded_env(config.get("env", {}), values))
    proc = subprocess.run([*capture, "--", *command], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=config.get("timeout_s", 900) + 60)
    (raw / "controller-capture.stdout.txt").write_text(proc.stdout)
    (raw / "controller-capture.stderr.txt").write_text(proc.stderr)
    if proc.returncode:
        raise RuntimeError(f"NCU capture failed for {candidate_id}; see {display(raw)}")

    reduce = [sys.executable, str(REDUCE), "--csv", str(raw / "metrics.csv"),
              "--capture-metadata", str(raw / "capture.json"), "--details-csv", str(raw / "details.csv"),
              "--source-csv", str(raw / "source.csv"), "--timeline-profile", str(timeline),
              "--run-id", plan["run_id"], "--phase", phase, "--output", str(output),
              "--diagnostics-output", str(diagnostics), "--drilldown-dir", str(drilldowns)]
    if candidate_id not in {"baseline", "final"}:
        reduce += ["--variant", candidate_id]
    reduced = subprocess.run(reduce, cwd=REPO, capture_output=True, text=True)
    (raw / "controller-reduce.stdout.txt").write_text(reduced.stdout)
    (raw / "controller-reduce.stderr.txt").write_text(reduced.stderr)
    if reduced.returncode or not output.is_file() or not diagnostics.is_file():
        raise RuntimeError(f"NCU reduction failed for {candidate_id}; see {display(raw)}")
    for schema, document in (("profile.schema.json", output),
                             ("ncu-diagnostics.schema.json", diagnostics)):
        checked = subprocess.run([sys.executable, str(REPO / "shared/scripts/validate.py"),
                                  str(REPO / "shared/schema" / schema), str(document)],
                                 cwd=REPO, capture_output=True, text=True)
        (raw / f"validate-{schema}.stdout.txt").write_text(checked.stdout)
        (raw / f"validate-{schema}.stderr.txt").write_text(checked.stderr)
        if checked.returncode:
            raise RuntimeError(f"invalid NCU artifact for {candidate_id}; see {display(raw)}")
    write(cache, {"protocol": "profileweaver.ncu-cache/v1", "signature": signature,
                  "evidence_fingerprint": fingerprint, "profile_sha256": sha256(output),
                  "diagnostics_sha256": sha256(diagnostics)})
    capsule = compact_ncu(load(output, {})) or {}
    return {"status": "complete", "cache": "miss", "disposition": "captured",
            "reason": reason or ("baseline" if candidate_id == "baseline" else
                                  "accepted-winner-fresh-replay" if candidate_id == "final" else
                                  "kernel-evidence-fingerprint-changed"),
            "spent_s": capsule.get("total_s"), "evidence_fingerprint": fingerprint,
            "evidence_inputs": inputs, "artifact": display(output), **capsule}


def candidate_kernel_profile(plan: dict, run: Path, candidate_id: str, workspace: Path,
                             timeline: Path, parent_id: str, parent: dict | None,
                             force: bool = False) -> dict:
    fingerprint, inputs = evidence_fingerprint(plan, run, candidate_id, workspace, parent)
    if not force and parent and parent.get("evidence_fingerprint") == fingerprint:
        excluded = {"delta", "delta_baseline", "delta_parent", "cache", "disposition", "reason",
                    "reused_from", "artifact", "source_artifact", "spent_s", "evidence_inputs"}
        capsule = {key: value for key, value in parent.items() if key not in excluded}
        reuse = candidate_dir(run, candidate_id) / "profile/ncu-reuse.json"
        source = parent.get("source_artifact") or parent.get("artifact")
        capsule.update({"status": "complete", "cache": "reused", "disposition": "reused",
                        "reason": "kernel-evidence-fingerprint-unchanged", "reused_from": parent_id,
                        "spent_s": 0, "evidence_fingerprint": fingerprint,
                        "evidence_inputs": inputs, "source_artifact": source,
                        "artifact": display(reuse)})
        write(reuse, {"protocol": "profileweaver.ncu-reuse/v1", "candidate": candidate_id,
                      "reused_from": parent_id, "source_artifact": source,
                      "evidence_fingerprint": fingerprint})
        return capsule
    return run_kernel_profile(plan, run, candidate_id, workspace, timeline, "variant", force,
                              "agent-forced-replay" if force else
                              "parent-kernel-evidence-unavailable" if not parent else None,
                              diagnostic=parent)
