"""Build/probe and run a replay-free CUPTI activity evidence provider."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import shlex
import subprocess
import time
from pathlib import Path

from experiment_support import REPO, display, expand, expanded_env, load, resolve, sha256, write
from campaign_io import check_oracle, extract
from validate import errors_for
from cupti_regions import region
from acquisition_limits import bounded, remaining


_COLLECTOR_STATUS = {}


def collector_status():
    return dict(_COLLECTOR_STATUS)


def collector(plan=None) -> Path | None:
    from cuda_layout import for_plan
    layout = for_plan(plan)
    _COLLECTOR_STATUS.clear()
    _COLLECTOR_STATUS.update(layout)
    cxx = shutil.which("g++")
    if layout["status"] != "available" or not cxx:
        if not cxx:
            _COLLECTOR_STATUS.update(status="unavailable", reason="host-cxx-unavailable")
        return None
    include = Path(layout["cupti_include"])
    library = Path(layout["cupti_library_dir"])
    source = REPO / "shared/native/cupti_activity.cpp"
    header = include / "cupti_activity.h"
    records = re.findall(r"}\s*(CUpti_ActivityKernel(\d+))\s*;", header.read_text())
    if not records:
        _COLLECTOR_STATUS.update(status="unavailable", reason="cupti-kernel-record-type-unresolved")
        return None
    record_type = max(records, key=lambda row: int(row[1]))[0]
    _COLLECTOR_STATUS["kernel_record_type"] = record_type
    lib = (library / "libcupti.so").resolve()
    signature = hashlib.sha256((sha256(source) + sha256(header) + sha256(Path(cxx).resolve()) +
        str(lib) + str(lib.stat().st_mtime_ns)).encode()).hexdigest()[:20]
    root = REPO / "runs/_cache/cupti" / signature
    output = root / "activity.so"
    if output.is_file():
        return output
    if (root / "unavailable.json").is_file():
        _COLLECTOR_STATUS.update(load(root / "unavailable.json"), status="unavailable", artifact=str(root))
        return None
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f"activity.{os.getpid()}.tmp.so"
    command = [cxx, "-std=c++17", "-O2", "-shared", "-fPIC", "-pthread", str(source),
               "-DPW_CUPTI_KERNEL_RECORD="+record_type,
               "-I"+str(include), "-I"+layout["cuda_include"], "-L"+str(library),
               "-Wl,-rpath,"+str(library), "-lcupti", "-o", str(temporary)]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=remaining(60))
    except (OSError, subprocess.TimeoutExpired) as exc:
        write(root / "unavailable.json", {"reason": type(exc).__name__})
        _COLLECTOR_STATUS.update(status="unavailable", reason=type(exc).__name__, artifact=str(root))
        return None
    (root / "build.stdout.txt").write_bytes(proc.stdout)
    (root / "build.stderr.txt").write_bytes(proc.stderr)
    if proc.returncode:
        temporary.unlink(missing_ok=True)
        write(root / "unavailable.json", {"reason": "collector-build-failed", "returncode": proc.returncode})
        _COLLECTOR_STATUS.update(status="unavailable", reason="collector-build-failed", artifact=str(root))
    else:
        temporary.replace(output)
    return output if proc.returncode == 0 else None


from resource_meter import capture_cost


@capture_cost("pw-cupti")
@bounded
def capture(plan: dict, run: Path, candidate_id: str, workspace: Path, *, phase=None, needs=None) -> dict:
    library = collector(plan)
    if not library or os.environ.get("CUDA_INJECTION64_PATH"):
        return {"id": candidate_id, "status": "blocked", "reason": "cupti-unavailable-or-injection-conflict"}
    raw = run / "raw/cupti" / ("final" if phase == "reprofile" else candidate_id)
    raw.mkdir(parents=True, exist_ok=True)
    for old in raw.glob("activity.*.json"):
        old.unlink()
    values = {"workspace": str(workspace), "run_dir": str(run), "repo": str(REPO),
              "run_id": plan["run_id"], "id": candidate_id, "sample": "0", "label": "cupti"}
    execution = plan["execution"]
    env = os.environ.copy() | expanded_env(plan.get("env", {}), values)
    env |= expanded_env(execution.get("env", {}), values)
    oracle_env = env.copy()
    roi_name = (plan.get("profiling") or {}).get("roi_name")
    if needs is not None and "roi-attribution" not in needs:
        from evidence_scope import requires_roi
        if not requires_roi(plan):
            roi_name = None
    if roi_name:
        from cuda_layout import for_plan
        nvtx_library = Path(for_plan(plan)["cupti_library"])
        if env.get("NVTX_INJECTION64_PATH") and Path(env["NVTX_INJECTION64_PATH"]).resolve() != nvtx_library.resolve():
            return {"id": candidate_id, "status": "blocked", "reason": "existing-nvtx-injection-preserved"}
        env |= {"PW_CUPTI_ROI": roi_name, "NVTX_INJECTION64_PATH": str(nvtx_library)}
    env |= {"CUDA_INJECTION64_PATH": str(library), "PW_CUPTI_OUTPUT": str(raw / "activity")}
    started = time.perf_counter()
    from experiment_bootstrap import _run
    proc = _run([expand(x, values) for x in execution["command"]],
                resolve(execution.get("cwd", "{workspace}"), values), env,
                raw / "capture.stdout.txt", raw / "capture.stderr.txt", remaining(execution.get("timeout_s", 300)))
    elapsed = time.perf_counter()-started
    records = [load(path, {}) for path in sorted(raw.glob("activity.*.json"))]
    oracle = check_oracle(plan.get("oracle"), proc.returncode,
                          proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"),
                          resolve(execution.get("cwd", "{workspace}"), values), oracle_env, values, raw / "oracle")
    valid = bool(records) and proc.returncode == 0 and all(
        x.get("launches", 0) > 0 and x.get("dropped") == 0 and x.get("errors") == 0 for x in records) and oracle["passed"]
    kernels = {}
    for record in records:
        for item in record.get("kernels", []):
            row = kernels.setdefault(item["name"], {"name": item["name"], "calls": 0, "total_ns": 0,
                **{key: item[key] for key in ("first_grid", "first_block", "registers_per_thread") if key in item}})
            row["calls"] += item["calls"]
            row["total_ns"] += item["total_ns"]
    ranked = sorted(kernels.values(), key=lambda x: x["total_ns"], reverse=True)
    region_info = region(records[0], roi_name) if roi_name and len(records) == 1 else None
    if roi_name:
        valid = valid and region_info is not None and bool(region_info["kernels"])
    if region_info:
        ranked = region_info["kernels"]
    wall = region_info["wall_s"] if region_info else elapsed
    scope = region_info["scope"] if region_info else {"policy": "whole-process-gpu-activities"}
    output = (run / "final-cupti/cupti-activity.json" if phase == "reprofile" else
              run / "candidates" / candidate_id / "profile/cupti-activity.json")
    evidence = {"protocol": "profileweaver.cupti-surface/v1", "status": "complete" if valid else "inconclusive",
                "scope": scope, "elapsed_s": elapsed,
                "collector_sha256": sha256(library), "records": records,
                "oracle": oracle,
                "limitations": (["Observed NVTX time bounds attribution; it does not prove synchronization semantics."] if region_info else
                                ["Summed kernel durations are not GPU busy union or ROI wall time."]) + [
                                "No cache, stalls, achieved occupancy, or host synchronization evidence."]}
    write(output, evidence)
    normalized = output.with_name("profile.activity.json")
    launch_basis = "first recorded launch in ROI" if region_info else "first process launch"
    profile = {"run_id": plan["run_id"], "phase": phase or ("baseline" if candidate_id == "baseline" else "variant"), "variant": None if candidate_id == "baseline" else candidate_id,
               "tool": {"name": "cupti-activity", "version": str((records or [{}])[0].get("cupti_version", "unknown")),
                        "mode": "concurrent-activity", "command": shlex.join([expand(x, values) for x in execution["command"]])},
               "workload": {"run_command": shlex.join(execution["command"])},
               "totals": {"wall_time_s": wall, "gpu_busy_s": region_info["gpu_busy_s"] if region_info else None, "attributed_s": None},
               "hotspots": [{"rank": i, "name": x["name"], "kind": "gpu-kernel",
                             "self_time_s": x["total_ns"] / 1e9, "calls": x["calls"],
                             **({"metrics": {"first_launch": {
                                 "grid": x["first_grid"], "block": x["first_block"], "basis": launch_basis}}}
                                if len(records) == 1 and x.get("first_grid") and x.get("first_block") else {}),
                             "pct_of_wall": x["total_ns"] / 1e9 / wall * 100}
                            for i, x in enumerate(ranked, 1)],
               "bottleneck": {"class": "hotspot", "scope": "nvtx-roi" if region_info else "whole-process",
                              "evidence": [{"metric": "kernel_launches", "value": sum(x.get("launches", 0) for x in records),
                                            "supports": "GPU activities were collected; no kernel-internal mechanism is inferred."}],
                              "confidence": "low"},
               "scope": scope,
               "warnings": [{"kind": "capability", "detail": x, "magnitude": None} for x in evidence["limitations"]],
               "raw_artifacts": [str(output)]}
    errors, _ = errors_for(load(REPO / "shared/schema/profile.schema.json", {}), profile)
    if errors:
        raise ValueError("invalid CUPTI normalized profile: " + "; ".join(errors[:3]))
    write(normalized, profile)
    try:
        diagnostic_metric = extract(plan["metric"], proc.stdout.decode("utf-8", "replace"),
                                    proc.stderr.decode("utf-8", "replace"), elapsed, values)
    except (KeyError, ValueError, OSError):
        diagnostic_metric = None
    return {"id": candidate_id, "status": evidence["status"], "surface": "pw-cupti",
            "scope": evidence["scope"], "artifact": display(output), "spent_s": elapsed,
            "observed_capabilities": ["gpu-hotspot", "transfer"] + (["roi-attribution"] if region_info else []),
            "reason": (None if valid else "roi-record-limit-exceeded"
                       if roi_name and any(x.get("phase_records_truncated") for x in records)
                       else "missing-complete-ROI-or-invalid-activity"),
            "target": ranked[0] if ranked else None,
            "launches": sum(x.get("launches", 0) for x in records),
            "copy_bytes": sum(x.get("copy_bytes", 0) for x in records),
            "copy_calls": sum(x.get("copy_calls", 0) for x in records),
            "dropped": sum(x.get("dropped", 0) for x in records),
            "errors": sum(x.get("errors", 0) for x in records),
            "correct": oracle["passed"], "profile_artifact": str(normalized),
            "diagnostic_metric": diagnostic_metric,
            "diagnostic_metric_role": "instrumented-not-for-ranking",
            "delta_baseline": None, "delta_parent": None}
