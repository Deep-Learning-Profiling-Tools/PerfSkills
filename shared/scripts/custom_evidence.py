"""Execute a declared profiler adapter and return its own bounded evidence surface."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from experiment_support import REPO, display, expand, expanded_env, load, resolve
from profile_delta import profile_variables
from validate import errors_for
from acquisition_limits import bounded, remaining
from bounded_process import run as run_process


@bounded
def capture(plan: dict, run: Path, candidate_id: str, workspace: Path,
            acquisition: dict) -> dict:
    adapter = plan["evidence_adapters"][acquisition["provider"]]
    variables = profile_variables(plan, run, candidate_id, workspace)
    raw = Path(variables["profile_raw"]) / acquisition["provider"]
    raw.mkdir(parents=True, exist_ok=True)
    Path(variables["profile_dir"]).mkdir(parents=True, exist_ok=True)
    output = Path(variables["profile_output"])
    if not output.is_relative_to(Path(variables["profile_dir"])):
        raise ValueError("custom profile must stay under the candidate profile directory")
    output.unlink(missing_ok=True)
    env = os.environ.copy() | expanded_env(plan.get("env", {}), variables)
    env |= expanded_env(plan.get("execution", {}).get("env", {}), variables)
    started = time.perf_counter()
    probes = adapter.get("probes") or []
    if not probes:
        raise ValueError("a custom evidence adapter requires capability probes")
    for i, stage in enumerate([*probes, *adapter["stages"]]):
        argv = [expand(x, variables) for x in stage["argv"]]
        proc = run_process(argv, resolve(stage.get("cwd", "{workspace}"), variables),
                           env | expanded_env(stage.get("env", {}), variables),
                           raw / f"{i}.stdout.txt", raw / f"{i}.stderr.txt", remaining(stage.get("timeout_s", 60)))
        if (proc.returncode or (stage.get("regex") and not re.search(
                stage["regex"], (proc.stdout + proc.stderr).decode(errors="replace")))):
            return {"id": candidate_id, "status": "blocked", "artifact": display(raw),
                    "reason": "profiler-capability-probe-or-capture-failed",
                    "spent_s": time.perf_counter() - started}
    document = load(output, {})
    if document.get("phase") != "variant" or document.get("variant") not in {None, candidate_id}:
        raise ValueError("custom evidence does not name this candidate's profile")
    document["variant"] = candidate_id
    errors, _ = errors_for(load(REPO / "shared/schema/profile.schema.json", {}), document)
    if errors:
        raise ValueError("invalid custom evidence profile: " + "; ".join(errors[:3]))
    from experiment_support import write
    write(output, document)
    top = (document.get("hotspots") or [{}])[0]
    evidence = document.get("provider_evidence") or {}
    capabilities = set(evidence.get("observed_capabilities") or []) & set(adapter.get("capabilities") or [])
    # Independent tool observations are not deltas against an NSYS/NCU capture.
    return {"id": candidate_id, "status": "complete", "surface": acquisition["provider"],
            "target": {key: top[key] for key in ("name", "file", "line", "self_time_s") if key in top},
            "observed_capabilities": sorted(capabilities), "provider_evidence": evidence,
            "delta_baseline": None, "delta_parent": None, "artifact": display(output),
            "profile_artifact": str(output), "scope": document.get("scope"),
            "spent_s": time.perf_counter() - started}
