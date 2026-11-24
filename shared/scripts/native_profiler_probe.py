"""Discover and capability-probe HPCToolkit/GPA without touching a workload.

Executable existence, advertised events, and reducer availability are separate
checks. PC support is never inferred from a negative mention of a PC event.
"""

from __future__ import annotations

from native_capabilities import advertised_events, gpa_models, model_matches_devices

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from acquisition_limits import remaining
import re
import shutil
import subprocess
import time

from experiment_support import REPO, load, write

PROVIDERS = {"pw-hpctoolkit", "pw-gpa"}


def settings(provider: str, plan: dict) -> dict:
    local = load(REPO / "shared/config/profilers.local.json", {}) or {}
    return ((local.get(provider) or {}) |
            (((plan.get("profiling") or {}).get("providers") or {}).get(provider) or {}))


def discover(provider: str, plan: dict, env=None) -> dict:
    env = os.environ if env is None else env
    config = settings(provider, plan)
    overrides = (plan.get("profiling") or {}).get("tool_paths") or {}
    root = config.get("root")
    if not root:
        root = (env.get("PW_GPA_TOOLCHAIN") or
                (str(Path(env["PW_GPA_ROOT"]) / "gpa") if env.get("PW_GPA_ROOT") else None)
                if provider == "pw-gpa" else
                env.get("PW_HPCTOOLKIT_ROOT") or env.get("HPCTOOLKIT_ROOT"))
    roots = [Path(root).expanduser()] if root else []
    result = {}
    for name in ("hpcrun", "hpcstruct", "hpcprof"):
        configured = config.get(name) or overrides.get(provider + "." + name) or overrides.get(name)
        candidates = ([Path(configured).expanduser()] if configured else [])
        for directory in roots:
            candidates += [directory / "bin" / name, directory / "hpctoolkit/bin" / name]
        found = next((str(p.resolve()) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
        result[name] = found or (shutil.which(name, path=env.get("PATH")) if not configured else None)
    return {"tools": result, "config": config}


def gpu_names(plan: dict, env: dict) -> list[str]:
    visible = ((plan.get("execution") or {}).get("env") or {}).get("CUDA_VISIBLE_DEVICES",
                (plan.get("env") or {}).get("CUDA_VISIBLE_DEVICES", env.get("CUDA_VISIBLE_DEVICES")))
    command = ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]
    if visible is not None:
        if not visible or "$" in str(visible) or "{" in str(visible):
            return []
        command += ["-i", str(visible)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=remaining(5), env=env)
        return proc.stdout.splitlines() if proc.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        return []


def _probe(argv: list[str], directory: Path, name: str, timeout=15, env=None) -> tuple[int | None, str]:
    # hpcrun -L can print millions of CPU-event characters. Preserve the complete
    # stream on disk and inspect only a bounded prefix containing GPU events/help.
    output = directory / (name + ".txt")
    try:
        with output.open("wb") as stream:
            proc = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=remaining(timeout), env=env)
        with output.open(errors="replace") as stream:
            text = ("".join(line for line in stream if any(word in line.lower() for word in
                     ("gpu=", "pc sampl", "realtime", "cuda support")))[:65536]
                    if name == "events" else stream.read(262144))
        return proc.returncode, text
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__


def probe(provider: str, plan: dict, env=None) -> dict:
    if provider not in PROVIDERS:
        raise ValueError("unknown native profiler")
    discovery = discover(provider, plan, env)
    tools, config = discovery["tools"], discovery["config"]
    missing = [name for name, value in tools.items() if not value]
    if missing:
        return {"status": "unavailable", "provider": provider, "capabilities": [],
                "reason": "missing-tools", "missing": missing, "tools": tools}
    probe_env = dict(os.environ if env is None else env) | (config.get("env") or {})
    devices = gpu_names(plan, probe_env) if provider == "pw-gpa" else []
    model_file = Path(config["source_root"]) / "src/lib/analysis/advisor/GPUArchitecture.hpp" if config.get("source_root") else None
    signature = {"revision": 3, "tools": {k: [v, Path(v).stat().st_size, Path(v).stat().st_mtime_ns]
                                         for k, v in tools.items()}, "config": config,
                 "model_source": gpa_models(config, "") if model_file and model_file.is_file() else None,
                 "gpu_devices": devices,
                 "python": os.sys.executable, "reducer_available": importlib.util.find_spec("hpcanalysis") is not None,
                 "environment": {k: probe_env.get(k) for k in
                                  ("LD_LIBRARY_PATH", "CUDA_HOME", "CUDA_PATH")}}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    directory = REPO / "runs/_cache/provider-probes" / key
    cached = load(directory / (provider + ".json"), {})
    if cached:
        if "struct_cache_supported" not in cached:
            help_path = directory / "hpcstruct-help.txt"
            cached["struct_cache_supported"] = "--cache" in (help_path.read_text(errors="replace") if help_path.exists() else "")
            write(directory / (provider + ".json"), cached)
        return cached | {"cache": "hit", "probe_s": 0}
    if (plan.get("profiling") or {}).get("probe_cache_only"):
        return {"status": "unavailable", "provider": provider, "capabilities": [],
                "reason": "capability-probe-not-cached-request-provider-explicitly", "probe_s": 0,
                "probe_artifact": str(directory)}
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    code, listing = _probe([tools["hpcrun"], "-L"], directory, "events", env=probe_env)
    version_code, version = _probe([tools["hpcrun"], "--version"], directory, "version", env=probe_env)
    help_code, help_text = _probe([tools["hpcprof"], "--help"], directory, "hpcprof-help", env=probe_env)
    struct_code, struct_help = _probe([tools["hpcstruct"], "--help"], directory, "hpcstruct-help", env=probe_env)
    events = advertised_events(listing) if code == 0 else {}
    models = gpa_models(config, help_text) if provider == "pw-gpa" else {}
    capabilities, reason = [], None
    if not events.get("coarse_event"):
        reason = "cuda-event-not-advertised"
    elif help_code != 0 or struct_code != 0:
        reason = "tool-help-probe-failed"
    elif provider == "pw-hpctoolkit":
        if importlib.util.find_spec("hpcanalysis") is None:
            reason = "hpcanalysis-reducer-missing"
        else:
            capabilities = ["gpu-hotspot", "transfer", "sync", "call-context"]
            if events.get("pc_event") and "--gpucfg" in struct_help:
                capabilities += ["gpu-source-line", "instruction-stalls", "memory-latency"]
    elif not events.get("pc_event"):
        reason = "pc-sampling-not-advertised-or-unsupported"
    elif "--gpu-arch" not in help_text:
        reason = "gpa-enabled-hpcprof-missing"
    elif not config.get("gpu_arch"):
        reason = "gpa-gpu-architecture-model-not-configured"
    elif config["gpu_arch"] not in models.get("models", []):
        reason = "gpa-gpu-architecture-model-unverified-or-unsupported"
    elif not model_matches_devices(config["gpu_arch"], devices):
        reason = "gpa-model-does-not-match-visible-gpu"
    else:
        capabilities = ["gpu-source-line", "instruction-stalls", "memory-latency",
                        "causal-stall-attribution", "gpu-sync-stalls"]
    result = {"provider": provider, "status": "available" if capabilities else "unavailable",
              "capabilities": capabilities, "reason": reason, "tools": tools, "events": events,
              "version": (version.splitlines() or ["unknown"])[0] if version_code == 0 else "unknown",
              "gpu_arch": config.get("gpu_arch"), "probe_artifact": str(directory),
              "architecture_models": models,
              "gpu_devices": devices,
              "probe_s": time.perf_counter() - started,
              "gpucfg_supported": "--gpucfg" in struct_help,
              "struct_jobs_supported": "-j" in struct_help,
              "struct_cache_supported": "--cache" in struct_help,
              "prof_jobs_supported": "-j" in help_text,
              "prof_no_source_supported": "--no-source" in help_text}
    write(directory / (provider + ".json"), result)
    return result
