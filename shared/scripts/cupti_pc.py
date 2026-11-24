"""Current CUPTI PC sampling without rebuilding or editing the application.

The SDK samples supply collection and decoding; their original source is never
modified. A cached copy fixes only output naming so profiles stay under raw/.
PC samples explain observed stalls, not GPA's causal/dataflow attribution.
"""

from __future__ import annotations

from cupti_pc_probe import CAPABILITIES, _stage, probe

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import uuid

from campaign_io import check_oracle, extract
from experiment_support import REPO, display, expand, expanded_env, load, resolve, sha256, write
from validate import errors_for
from acquisition_limits import bounded, remaining
from bounded_process import run as run_process


def reduce_records(paths, target_regex=None):
    rows, headers = [], []
    totals = Counter()
    for path in paths:
        with Path(path).open(errors="replace") as stream:
            for line in stream:
                header = re.search(r"Total Samples: (\d+), Total Dropped Samples: (\d+)", line)
                if header:
                    headers.append(tuple(map(int, header.groups()))); continue
                match = re.search(r"cubinCrc: (\d+), functionName: (.*?), functionIndex: (\d+), correlationId: (\d+), pcOffset: (\d+), stallReasonCount: (\d+)(.*)", line)
                if not match:
                    continue
                crc, name, index, correlation, offset, count, rest = match.groups()
                metrics = {k: int(v) for k, v in re.findall(r", ([A-Za-z0-9_]+): (\d+)", rest)}
                if len(metrics) != int(count):
                    raise ValueError("incomplete PC stall record")
                rows.append({"symbol": name, "cubin_crc": int(crc), "offset": int(offset), "metrics": metrics})
    names = sorted({x["symbol"] for x in rows})
    if names:
        proc = subprocess.run(["c++filt"], input="\n".join(names) + "\n", text=True, capture_output=True, timeout=remaining(10))
        decoded = proc.stdout.splitlines() if proc.returncode == 0 else names
        names = dict(zip(names, decoded))
    selected = [r for r in rows if not target_regex or re.search(target_regex, names.get(r["symbol"], r["symbol"]))]
    pcs = defaultdict(Counter)
    for row in selected:
        pcs[(names.get(row["symbol"], row["symbol"]), row["cubin_crc"], row["offset"])].update(row["metrics"])
        totals.update(row["metrics"])
    # *_not_issued is a subset of its parent metric; never add the two populations.
    primary = {k: v for k, v in totals.items() if not k.endswith("_not_issued")}
    denominator = sum(primary.values())
    stalls = [{"metric": k, "value": v, "unit": "PC-sampled-warps",
               "fraction": v / denominator if denominator else None}
              for k, v in sorted(primary.items(), key=lambda x: -x[1])]
    locations = [{"kernel": name, "cubin_crc": crc, "pc_offset": offset,
                  "metrics": dict(metrics), "file": None, "line": None}
                 for (name, crc, offset), metrics in sorted(pcs.items(),
                     key=lambda x: -sum(v for k, v in x[1].items() if not k.endswith("_not_issued")))]
    kernel_counts = Counter()
    for location in locations:
        kernel_counts[location["kernel"]] += sum(v for k, v in location["metrics"].items() if not k.endswith("_not_issued"))
    return {"stalls": stalls, "locations": locations, "samples": denominator,
            "reported_total_samples": sum(x[0] for x in headers), "dropped_samples": sum(x[1] for x in headers),
            "headers": len(headers), "kernels": dict(kernel_counts),
            "observed_capabilities": sorted(CAPABILITIES) if selected and denominator else [],
            "limitations": ["PC samples are diagnostic; no kernel duration, achieved occupancy or cache traffic is measured.",
                            "PC offset identifies the sampled instruction, not GPA's causal blame instruction; source lines are unresolved."]}


from resource_meter import capture_cost


@capture_cost("pw-cupti")
@bounded
def capture(plan: dict, run: Path, candidate_id: str, workspace: Path, acquisition: dict, *, validated=False) -> dict:
    capability = probe(plan)
    if not validated or capability["status"] != "available":
        return {"id": candidate_id, "status": "blocked", "reason": "application-not-validated" if not validated else capability["reason"]}
    raw = run / "raw/providers" / candidate_id / ("cupti-pc-" + uuid.uuid4().hex[:12]); raw.mkdir(parents=True)
    values = {"workspace": str(workspace), "run_dir": str(run), "repo": str(REPO),
              "run_id": plan["run_id"], "id": candidate_id, "sample": "0", "label": "cupti-pc"}
    execution = plan["execution"]
    env = os.environ.copy() | expanded_env(plan.get("env", {}), values) | expanded_env(execution.get("env", {}), values)
    if env.get("CUDA_INJECTION64_PATH") or env.get("INJECTION_PARAM"):
        return {"id": candidate_id, "status": "blocked", "reason": "existing-injection-conflict"}
    prefix = str(raw / "pcsampling.dat")
    if any(c.isspace() for c in prefix):
        return {"id": candidate_id, "status": "blocked", "reason": "sdk-output-path-contains-whitespace"}
    env.update({"CUDA_INJECTION64_PATH": capability["collector"],
                "INJECTION_PARAM": "--sampling-period 12 --hw-buf-size 67108864 --file-name " + prefix})
    command = [expand(x, values) for x in execution["command"]]
    cwd = resolve(execution.get("cwd", "{workspace}"), values)
    # Fingerprint the resolved executable when possible, including an MPI command.
    from native_profiler import executable
    try:
        binary = executable(plan, workspace, values)
    except (ValueError, OSError):
        return {"id": candidate_id, "status": "blocked", "reason": "application-executable-unresolved"}
    if binary is None:
        return {"id": candidate_id, "status": "blocked", "reason": "application-executable-unresolved"}
    binary_hash = sha256(binary)
    started = time.perf_counter(); stages = []
    stages.append(_stage(command, cwd, env, raw, "capture", execution.get("timeout_s", 300)))
    stdout, stderr = Path(stages[0]["stdout"]).read_text(errors="replace"), Path(stages[0]["stderr"]).read_text(errors="replace")
    oracle = check_oracle(plan.get("oracle"), stages[0]["returncode"], stdout, stderr, cwd, env, values, raw / "oracle")
    files = sorted(raw.glob("pcsampling.dat.*")); records = []
    for n, path in enumerate(files):
        stage = _stage([capability["decoder"], "--file-name", str(path), "--disable-source-correlation"],
                       raw, env, raw, f"decode-{n}", 60)
        stages.append(stage); records.append(stage["stdout"])
    evidence = reduce_records(records, acquisition.get("target_regex"))
    correct = oracle["passed"] and stages[0]["returncode"] == 0
    valid = (correct and files and evidence["headers"] > 0 and evidence["samples"] > 0
             and evidence["dropped_samples"] == 0 and all(x["returncode"] == 0 for x in stages)
             and binary_hash == sha256(binary))
    status = "complete" if valid else "inconclusive"
    metadata = {"status": status, "provider": "pw-cupti", "mode": "pc-sampling", "oracle": oracle,
                "stages": stages, "elapsed_s": time.perf_counter() - started, "probe": capability["artifact"],
                "binary": str(binary), "binary_sha256": binary_hash,
                "target_regex": acquisition.get("target_regex"), "target_scope": acquisition.get("target_scope"),
                "collection_scope": "whole-process-PC-samples", "ranking_role": "diagnostic-only"}
    write(raw / "capture.json", metadata); write(raw / "evidence.json", evidence)
    hotspots = [{"rank": n, "name": name, "kind": "gpu-kernel", "self_time_s": None,
                 "pct_of_wall": None, "file": None, "line": None, "metrics": {"pc_samples": count}}
                for n, (name, count) in enumerate(sorted(evidence["kernels"].items(), key=lambda x: -x[1]), 1)]
    # Compact reduction contains top PCs with literal metrics; complete PC rows remain raw.
    provider = {k: evidence[k] for k in ("stalls", "samples", "dropped_samples", "observed_capabilities", "limitations")}
    provider["stalls"] = [dict(row, **({"kernel": evidence["locations"][0]["kernel"]} if evidence["locations"] else {})) for row in provider["stalls"][:12]]
    provider["locations"] = evidence["locations"][:8]
    output = run / "candidates" / candidate_id / "profile/profile.cupti-pc.json"
    profile = {"run_id": plan["run_id"], "phase": "variant", "variant": candidate_id,
               "tool": {"name": "CUPTI PC sampling", "version": capability["version"], "mode": "pc-sampling", "command": shlex.join(command)},
               "workload": {"run_command": shlex.join(command)}, "totals": {"wall_time_s": None, "gpu_busy_s": None},
               "hotspots": hotspots, "bottleneck": {"class": "instruction-stalls", "scope": "sampled-instruction", "confidence": "low",
                   "evidence": [{"metric": r["metric"], "value": r["value"], "supports": "Observed sampled-warp stall state, not causal blame or application timing."} for r in evidence["stalls"][:3]]},
               "scope": {"policy": "whole-process-PC-collection-target-filtered-reduction", "rank_basis": "PC-sample-count-not-time"},
               "provider_evidence": provider, "warnings": [{"kind": "capability", "detail": x, "magnitude": None} for x in evidence["limitations"]],
               "raw_artifacts": [str(raw / "capture.json"), str(raw / "evidence.json")]}
    errors, _ = errors_for(load(REPO / "shared/schema/profile.schema.json", {}), profile)
    if errors:
        write(raw / "validation-errors.json", errors)
        return {"id": candidate_id, "status": "inconclusive", "reason": "normalized-profile-invalid", "artifact": display(raw / "capture.json")}
    write(output, profile)
    try:
        metric = extract(plan["metric"], stdout, stderr, stages[0]["elapsed_s"], values)
    except (KeyError, ValueError, OSError):
        metric = None
    return {"id": candidate_id, "status": status, "reason": None if valid else "capture-or-oracle-or-sample-quality-failed",
            "surface": "pw-cupti-pc", "profile_artifact": str(output), "artifact": display(raw / "capture.json"),
            "spent_s": metadata["elapsed_s"], "correct": correct, "target": hotspots[0] if hotspots else None,
            "scope": profile["scope"], "provider_evidence": provider,
            "observed_capabilities": evidence["observed_capabilities"] if valid else [],
            "diagnostic_metric": metric, "diagnostic_metric_role": "instrumented-not-for-ranking",
            "delta_baseline": None, "delta_parent": None}
