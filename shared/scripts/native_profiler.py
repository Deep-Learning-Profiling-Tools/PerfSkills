"""One bounded HPCToolkit/GPA capture of the already validated executable.

No corpus registry, application rebuild, optimizer, or source edits are invoked.
Each attempt keeps its commands and outputs. Tool predictions never rank code.
"""

from __future__ import annotations

from native_launcher import executable, instrument

import os
import shlex
from pathlib import Path
from acquisition_limits import bounded, remaining
from bounded_process import run as run_process
import subprocess
import sys
import time
import uuid

from campaign_io import check_oracle, extract
from experiment_support import REPO, display, expand, expanded_env, load, resolve, sha256, write
from native_profiler_probe import probe, settings
from validate import errors_for


def _stage(argv: list[str], cwd: Path, env: dict, raw: Path, name: str, timeout: float) -> dict:
    started = time.perf_counter()
    stdout, stderr = raw / (name + ".stdout.txt"), raw / (name + ".stderr.txt")
    try:
        result = run_process(argv, cwd, env, stdout, stderr, remaining(timeout))
        code, error = result.returncode, None
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        code, error = None, type(exc).__name__
    return {"stage": name, "command": argv, "cwd": str(cwd), "returncode": code,
            "elapsed_s": time.perf_counter() - started, "error": error,
            "stdout": str(stdout), "stderr": str(stderr)}


from resource_meter import capture_cost


@capture_cost("native")
@bounded
def capture(plan: dict, run: Path, candidate_id: str, workspace: Path, acquisition: dict,
            *, validated: bool = False) -> dict:
    provider = acquisition["provider"]
    if not validated:
        return {"id": candidate_id, "status": "blocked", "reason": "application-not-validated"}
    capability = probe(provider, plan)
    needs = acquisition.get("needs") or []
    if capability["status"] != "available" or not set(needs).issubset(capability["capabilities"]):
        return {"id": candidate_id, "status": "blocked", "reason": "native-capability-unavailable",
                "capability_probe": capability, "spent_s": 0}
    config, tools = settings(provider, plan), capability["tools"]
    variables = {"workspace": str(workspace), "run_dir": str(run), "run_id": plan["run_id"],
                 "repo": str(REPO), "id": candidate_id, "sample": "0", "label": provider}
    binary = executable(plan, workspace, variables, config.get("executable"))
    if binary is None:
        return {"id": candidate_id, "status": "blocked", "reason": "application-ELF-not-resolved", "spent_s": 0}
    pc = provider == "pw-gpa" or any(n in needs for n in ("gpu-source-line", "instruction-stalls", "memory-latency"))
    event = capability["events"].get("pc_event" if pc else "coarse_event")
    if not event:
        return {"id": candidate_id, "status": "blocked", "reason": "requested-sampling-event-unavailable", "spent_s": 0}
    raw = run / "raw/providers" / candidate_id / provider / uuid.uuid4().hex[:12]
    raw.mkdir(parents=True)
    measurements, database = raw / "measurements", raw / "database"
    cwd = resolve(plan["execution"].get("cwd", "{workspace}"), variables)
    env = os.environ.copy() | expanded_env(plan.get("env", {}), variables)
    env |= expanded_env(plan["execution"].get("env", {}), variables)
    env |= expanded_env(config.get("env", {}), variables)
    if capability.get("struct_cache_supported") and not env.get("HPCTOOLKIT_HPCSTRUCT_CACHE"):
        cache = REPO / "runs/_cache/hpcstruct" / provider / ("pc" if pc else "coarse")
        cache.mkdir(parents=True, exist_ok=True)
        env["HPCTOOLKIT_HPCSTRUCT_CACHE"] = str(cache)
    command = [expand(x, variables) for x in plan["execution"]["command"]]
    prefix = [tools["hpcrun"], "-e", "REALTIME@5000", "-e", event, "-o", str(measurements)]
    try:
        capture_command = instrument(command, binary, prefix, cwd)
    except ValueError:
        blocked = {"id": candidate_id, "status": "blocked", "surface": provider,
                   "reason": "launcher-placement-unresolved", "spent_s": 0,
                   "artifact": display(raw / "capture.json")}
        write(raw / "capture.json", blocked | {"command": command, "binary": str(binary)})
        return blocked
    structure = [tools["hpcstruct"]]
    if capability.get("struct_jobs_supported"):
        structure += ["-j", str(config.get("jobs", 4))]
    if capability.get("gpucfg_supported"):
        structure += ["--gpucfg", "yes" if pc else "no"]
    structure += [str(measurements)]
    analysis = [tools["hpcprof"]]
    if capability.get("prof_jobs_supported"):
        analysis += ["-j", str(config.get("jobs", 4))]
    if capability.get("prof_no_source_supported"):
        analysis += ["--no-source"]
    commands = [("capture", capture_command, config.get("capture_timeout_s", 1200)),
                ("structure", structure, config.get("structure_timeout_s", 900))]
    if provider == "pw-gpa":
        binary_struct = raw / "application.hpcstruct"
        commands += [("binary-structure", [tools["hpcstruct"], str(binary), "-o", str(binary_struct)],
                      config.get("structure_timeout_s", 900))]
        analysis += ["--gpu-arch", capability["gpu_arch"], "-S", str(binary_struct)]
    analysis += ["-o", str(database), str(measurements)]
    commands.append(("analysis", analysis, config.get("analysis_timeout_s", 900)))
    stages, started = [], time.perf_counter()
    metadata = {"provider": provider, "event": event, "binary": str(binary),
                "binary_sha256": sha256(binary),
                "structure_cache": env.get("HPCTOOLKIT_HPCSTRUCT_CACHE"),
                "capability_probe": capability, "stages": stages, "ranking_role": "diagnostic-only"}

    def finish(status, reason=None):
        metadata.update({"status": status, "reason": reason, "elapsed_s": time.perf_counter() - started})
        write(raw / "capture.json", metadata)
        return {"id": candidate_id, "status": status, "reason": reason, "surface": provider,
                "artifact": display(raw / "capture.json"), "spent_s": metadata["elapsed_s"]}

    for name, argv, timeout in commands:
        stage = _stage(argv, cwd, env, raw, name, timeout)
        stages.append(stage)
        if stage["returncode"] != 0:
            return finish("blocked", name + "-failed")
        if name == "capture":
            stdout, stderr = Path(stage["stdout"]).read_text(errors="replace"), Path(stage["stderr"]).read_text(errors="replace")
            oracle = check_oracle(plan.get("oracle"), 0, stdout, stderr, cwd, env, variables, raw / "oracle")
            metadata["oracle"] = oracle
            if not oracle["passed"]:
                return finish("inconclusive", "profiled-application-correctness-failed")
            try:
                metadata["diagnostic_metric"] = extract(plan["metric"], stdout, stderr, stage["elapsed_s"], variables)
            except (KeyError, ValueError, OSError):
                metadata["diagnostic_metric"] = None
    evidence_path = raw / "evidence.json"
    reducer = [sys.executable, str(REPO / "shared/scripts/native_profiler_reduce.py"),
               "--output", str(evidence_path)]
    reducer += (["--advice", str(database / "gpa.advice")] if provider == "pw-gpa" else
                ["--database", str(database)])
    if provider == "pw-gpa" and acquisition.get("target_regex"):
        reducer += ["--kernel-regex", acquisition["target_regex"]]
    stages.append(_stage(reducer, REPO, env, raw, "reduce", config.get("reduce_timeout_s", 120)))
    if stages[-1]["returncode"] != 0:
        return finish("inconclusive", "reduction-failed")
    evidence = load(evidence_path, {})
    if sha256(binary) != metadata["binary_sha256"]:
        return finish("inconclusive", "application-binary-changed-during-capture")
    if not evidence.get("hotspots"):
        return finish("inconclusive", "no-actionable-gpu-records")
    output = run / "candidates" / candidate_id / "profile" / (provider + ".json")
    stall_record = next((f for f in evidence.get("findings", []) if f.get("stall_token")), None)
    stall_record = stall_record or next(iter(evidence.get("stalls", [])), None)
    bottleneck_evidence = ({"metric": stall_record.get("stall_token") or stall_record["metric"],
                           "value": stall_record.get("reported_ratio_pct", stall_record.get("value")),
                           "supports": "Literal provider stall evidence; no application speedup is inferred."}
                          if stall_record else {"metric": "native_records", "value": len(evidence["hotspots"]),
                           "supports": "Provider records identify attributed GPU contexts; application timing is separate."})
    document = {"run_id": plan["run_id"], "phase": "baseline" if candidate_id == "baseline" else "variant", "variant": None if candidate_id == "baseline" else candidate_id,
        "tool": {"name": "GPU Performance Advisor" if provider == "pw-gpa" else "HPCToolkit",
                 "version": capability.get("version", "unknown"), "mode": "PC-sampling" if pc else "GPU-operation-context",
                 "command": shlex.join(capture_command)},
        "workload": {"run_command": " ".join(plan["execution"]["command"])},
        "totals": {"wall_time_s": None, "gpu_busy_s": None}, "hotspots": evidence["hotspots"],
        "bottleneck": {"class": "instruction-stalls" if stall_record else "hotspot" if not pc else "unclassified",
                       "scope": "whole-process", "confidence": "low", "evidence": [bottleneck_evidence]},
        "scope": {"policy": "whole-process-context" if not pc else "sampled-instruction-attribution",
                  "timing_join": "independent-tool-no-cross-tool-deltas"},
        "warnings": [{"kind": "capability", "detail": text, "magnitude": None} for text in evidence.get("limitations", [])],
        "provider_evidence": evidence, "raw_artifacts": [str(raw / "capture.json"), str(database)]}
    errors, _ = errors_for(load(REPO / "shared/schema/profile.schema.json", {}), document)
    if errors:
        metadata["validation_errors"] = errors[:3]
        return finish("inconclusive", "normalized-profile-invalid")
    write(output, document)
    unanswered = sorted(set(needs) - set(evidence.get("observed_capabilities", [])))
    result = finish("partial" if unanswered else "complete", "requested-metrics-not-recorded" if unanswered else None)
    return result | {"profile_artifact": str(output), "scope": document["scope"],
        "target": evidence["hotspots"][0], "provider_evidence": evidence,
        "observed_capabilities": evidence.get("observed_capabilities", []),
        "unanswered": unanswered,
        "diagnostic_metric": metadata.get("diagnostic_metric"), "delta_baseline": None, "delta_parent": None}
