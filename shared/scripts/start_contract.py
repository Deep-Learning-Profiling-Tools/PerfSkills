"""Create ProfileWeaver's internal plans from one workload manifest."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from experiment_support import REPO


AUTO_KERNEL = "__PROFILEWEAVER_AUTO_KERNEL__"


def _tool(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ValueError(f"required command is unavailable: {name}")
    return str(Path(found).resolve())


def tools() -> dict[str, str]:
    return {name: _tool(name) for name in ("nvidia-smi", "nvcc", "nsys", "ncu")}


def substitute(value, facts: dict[str, str]):
    if isinstance(value, str):
        for key, replacement in facts.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, list):
        return [substitute(item, facts) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, facts) for key, item in value.items()}
    return value


def preflight(run_id: str, run: Path, source: Path, found: dict[str, str]) -> dict:
    checks = [
        {"id": "device", "kind": "device", "argv": [found["nvidia-smi"],
         "--query-gpu=name,compute_cap", "--format=csv,noheader"],
         "expect": {"returncode": 0, "regex": r"\S"}},
        {"id": "compiler", "kind": "compiler", "argv": [found["nvcc"], "--version"],
         "expect": {"returncode": 0, "regex": "release"}},
        {"id": "nsys-version", "kind": "profiler", "argv": [found["nsys"], "--version"],
         "expect": {"returncode": 0, "regex": "Nsight Systems"}},
        {"id": "ncu-version", "kind": "profiler", "argv": [found["ncu"], "--version"],
         "expect": {"returncode": 0, "regex": "Nsight Compute"},
         "extract": [{"name": "ncu_version", "regex": r"Version\s+([^\n]+)"}]},
        {"id": "ncu-sections", "kind": "profiler", "argv": [found["ncu"], "--list-sections"],
         "expect": {"returncode": 0, "regex": "LaunchStats"}},
    ]
    return {"version": 1, "run_id": run_id, "run_dir": str(run),
            "required_tools": list(found.values()),
            "fingerprint_inputs": [str(source)], "checks": checks}


def _profile_stages(found: dict[str, str], roi: str, command: list[str]) -> list[dict]:
    reducer = REPO / ".claude/skills/pw-nsight-systems/scripts/reduce_sqlite.py"
    locator = REPO / "shared/scripts/locate_source.py"
    return [
        {"id": "nsys-capture", "argv": [found["nsys"], "profile", "--trace=cuda,nvtx",
         "--sample=none", "--cpuctxsw=none", "--stats=false", "--force-overwrite=true",
         "-o", "{profile_raw}/capture", *command],
         "cwd": "{workspace}", "produces": ["{profile_raw}/capture.nsys-rep"]},
        {"id": "nsys-export", "argv": [found["nsys"], "export", "--type", "sqlite",
         "--force-overwrite=true", "-o", "{profile_raw}/capture.sqlite",
         "{profile_raw}/capture.nsys-rep"], "cwd": "{workspace}",
         "produces": ["{profile_raw}/capture.sqlite"]},
        {"id": "nsys-reduce", "argv": ["python3", str(reducer), "--sqlite",
         "{profile_raw}/capture.sqlite", "--run-id", "{run_id}", "--phase", "{profile_phase}",
         "--roi-name", roi, "--question-class", "hotspot", "--preflight",
         "{run_dir}/preflight-result.json", "--output", "{profile_output}",
         "--details-output", "{profile_dir}/roi-metrics.json"], "cwd": "{repo}",
         "fingerprint_inputs": [str(reducer)],
         "produces": ["{profile_output}", "{profile_dir}/roi-metrics.json"]},
        {"id": "locate", "argv": ["python3", str(locator), "--profile", "{profile_output}",
         "--run-dir", "{profile_dir}", "--source-root", "{workspace}", "--repo-root",
         "{repo}", "--output", "{profile_dir}/source-targets.json", "--max-targets", "4",
         "--max-variants", "3", "--dependency-depth", "3", "--max-dependencies", "9"], "cwd": "{repo}",
         "fingerprint_inputs": [str(locator)],
         "produces": ["{profile_dir}/source-targets.json"]},
    ]


def _final_stages(found: dict[str, str], roi: str, command: list[str]) -> list[dict]:
    reducer = REPO / ".claude/skills/pw-nsight-systems/scripts/reduce_sqlite.py"
    raw = "{run_dir}/raw/final"
    return [
        {"id": "nsys-capture", "argv": [found["nsys"], "profile", "--trace=cuda,nvtx",
         "--sample=none", "--cpuctxsw=none", "--stats=false", "--force-overwrite=true",
         "-o", f"{raw}/capture", *command], "cwd": "{workspace}",
         "produces": [f"{raw}/capture.nsys-rep"]},
        {"id": "nsys-export", "argv": [found["nsys"], "export", "--type", "sqlite",
         "--force-overwrite=true", "-o", f"{raw}/capture.sqlite", f"{raw}/capture.nsys-rep"],
         "cwd": "{workspace}", "produces": [f"{raw}/capture.sqlite"]},
        {"id": "nsys-reduce", "argv": ["python3", str(reducer), "--sqlite",
         f"{raw}/capture.sqlite", "--run-id", "{run_id}", "--phase", "reprofile",
         "--variant", "{winner}", "--roi-name", roi, "--question-class", "hotspot",
         "--preflight", "{run_dir}/preflight-result.json", "--output",
         "{run_dir}/profile.reprofile.json", "--details-output",
         "{run_dir}/roi-metrics.reprofile.json"], "cwd": "{repo}",
         "produces": ["{run_dir}/profile.reprofile.json",
                      "{run_dir}/roi-metrics.reprofile.json"]},
    ]


def experiment(manifest: dict, run_id: str, run: Path, baseline: Path,
               found: dict[str, str], iterations: int, repairs: int) -> dict:
    execution = manifest["execution"]
    profiling = manifest.get("profiling") or {}
    pattern = profiling.get("kernel_regex") or AUTO_KERNEL
    command = execution["command"]
    sampling = manifest.get("sampling") or {"mode": "standard", "warmup": 1,
        "min_pairs": 5, "max_pairs": 7, "final_min_pairs": 7, "final_max_pairs": 11,
        "finalists": 2, "bootstrap_iterations": 1000, "practical_threshold": 1.02,
        "wall_gate": "advisory"}
    plan = {
        "version": 1, "run_id": run_id, "run_dir": str(run),
        "question": manifest.get("question") or "Optimize the largest measured GPU hotspot.",
        "budget": {"iterations": iterations, "repairs_per_iteration": repairs},
        "env": manifest.get("env", {}), "preflight_plan": str(run / "preflight-plan.json"),
        "source": {"baseline_dir": str(baseline),
                   "fingerprint_globs": manifest["fingerprint_globs"],
                   "copy_ignore": manifest.get("copy_ignore", [])},
        "execution": execution, "metric": manifest["metric"], "oracle": manifest.get("oracle"),
        "sampling": sampling,
        "profiling": {"discovery_needs": ["gpu-hotspot"],
                      "diagnostic_needs": ["gpu-occupancy", "memory-bandwidth", "memory-latency",
                                           "instruction-stalls", "register-pressure", "gpu-roofline"],
                      "available": ["pw-nsight-systems", "pw-nsight-compute"],
                      "tool_paths": {"nsys": found["nsys"], "ncu": found["ncu"]}},
        "attribution": {"baseline_target_regex": pattern, "variant_target_regex": pattern,
                        "metric": "self_time_s", "direction": "lower"},
        "iteration_profile": {"baseline_profile": "{run_dir}/profile.baseline.json",
            "profile_output": "{profile_dir}/profile.json",
            "source_targets": "{profile_dir}/source-targets.json",
            "cadence": profiling.get("cadence", "baseline-final"),
            "max_delta_items": 2,
            "stages": _profile_stages(found, profiling.get("roi_name", "hpcperf:roi"),
                                      command)},
        "kernel_profile": {"enabled": True, "section": "all", "candidate_sections": "auto",
            "executable": found["ncu"], "baseline_kernel_regex": pattern,
            "variant_kernel_regex": pattern, "launch_count": 1,
            "timeout_s": profiling.get("ncu_timeout_s", 1800),
            "fingerprint_inputs": manifest.get("kernel_fingerprint_inputs") or [command[0]]},
        "kernel_timing": {"source": "ncu-duration", "detail": "filtered hotspot kernel"},
        "finalize": {"run_finalizer": True,
                     "stages": _final_stages(found, profiling.get("roi_name", "hpcperf:roi"),
                                              command)},
    }
    plan["kernel_profile"]["final_timeline_profile"] = "{run_dir}/profile.reprofile.json"
    return plan


def selected_kernel(profile: dict) -> str:
    hotspot = next((item for item in profile.get("hotspots", [])
                    if item.get("kind") == "gpu-kernel"), None)
    if not hotspot or not hotspot.get("name"):
        raise ValueError("NSYS baseline did not identify a GPU kernel")
    return "^" + re.escape(hotspot["name"]) + "$"
