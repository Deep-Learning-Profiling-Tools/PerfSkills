"""Bind profiler recipes to observed tools, devices and implementation."""

import hashlib
import json
import os
import shutil
from pathlib import Path
from experiment_support import REPO, load, write


def enabled(plan):
    return ((plan.get("profiling") or {}).get("skill_runtime") or {}).get("enabled", False)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def binding(plan):
    """Bind observed versions, tool files, environment and adapter implementation.

    No benchmark name, source optimization, or measured speedup enters this key.
    Presence here is not a capability probe; routing must still positively probe.
    """
    run = Path(plan["run_dir"])
    provenance = load(run / "startup-provenance.json", {})
    tools = dict(provenance.get("tools") or {})
    tools.update((plan.get("profiling") or {}).get("tool_paths") or {})
    tools.update({name: value for name in ("hpcrun", "hpcstruct", "hpcprof")
                  if (value := shutil.which(name))})
    stamps = {}
    for name, value in tools.items():
        if not isinstance(value, str):
            continue
        path = Path(value).resolve()
        try:
            stat = path.stat()
            stamps[name] = [str(path), stat.st_size, stat.st_mtime_ns]
        except OSError:
            stamps[name] = [str(path), None, None]
    implementation = {name: hashlib.sha256((REPO / "shared/scripts" / name).read_bytes()).hexdigest()
                      for name in ("evidence_router.py", "cupti_activity.py", "cupti_pc.py",
                                   "native_profiler.py", "native_launcher.py", "native_capabilities.py",
                                   "cupti_pc_probe.py", "kernel_runner.py", "kernel_capture_contract.py", "kernel_identity.py",
                                   "profile_policy.py", "skill_runtime.py", "skill_binding.py", "skill_experience.py")}
    runtime = (plan.get("profiling") or {}).get("skill_runtime") or {}
    if runtime.get("decision_policy") in {"budgeted-lookahead-v2", "budgeted-lookahead-v3"}:
        for name in ("rewrite_evidence", "contingent_policy", "resource_meter", "resource_inputs", "evidence_dependencies", "evidence_presentation",
                     "selection_objective", "objective_acquisition", "kernel_selection_report", "objective_comparison", "diagnostic_context",
                     "shared_activity_measurement", "activity_context", "change_manifest", "campaign_report",
                     "capture_reuse", "iteration_context", "screen_outcome", "packet_layout", "changed_sources",
                     "baseline_reuse", "profile_observations", "locate_source", "source_symbols", "source_scan", "source_variants"):
            implementation[name] = hashlib.sha256((REPO / "shared/scripts" / (name + ".py")).read_bytes()).hexdigest()
        collector = REPO / "shared/native/cupti_activity.cpp"
        implementation[collector.name] = hashlib.sha256(collector.read_bytes()).hexdigest()
    if runtime.get("decision_policy") in {"marginal-value-v1", "budgeted-lookahead-v2", "budgeted-lookahead-v3"}:
        for name in ("decision_policy", "decision_learning", "acquisition_value", "iteration_packet",
                     "iteration_evidence", "terminal_view", "capture_history", "evidence_views",
                     "phase_opportunity", "timeline_summary", "application_phases",
                     "compiler_cache"):
            implementation[name] = hashlib.sha256((REPO / "shared/scripts" / (name + ".py")).read_bytes()).hexdigest()
    observed = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (run / "raw/preflight").glob("*.stdout.txt")
                if p.name in {"compiler.stdout.txt", "device.stdout.txt", "ncu-version.stdout.txt", "nsys-version.stdout.txt"}}
    material = {"tools": stamps, "observed": observed, "gpu": provenance.get("gpu_inventory"),
                "declared_environment": provenance.get("declared_environment"),
                "inherited_environment": provenance.get("inherited_environment"),
                "runtime_environment": {k: os.environ.get(k) for k in
                    ("CUDA_VISIBLE_DEVICES", "CUDA_INJECTION64_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH")},
                "implementation": implementation,
                "custom_adapters": plan.get("evidence_adapters"),
                "kernel_sections": (plan.get("kernel_profile") or {}).get("candidate_sections")}
    return {"key": digest(material), "material": material,
            "observed": bool(observed or provenance.get("gpu_inventory"))}


def context(plan, state):
    """Coarse measured workload nature, not benchmark identity or a code recipe."""
    run = Path(plan["run_dir"])
    profile = load(run / "profile.baseline.json", {})
    top = (profile.get("hotspots") or [{}])[0]
    fraction = (top.get("pct_of_wall") or 0) / 100
    duration = (profile.get("totals") or {}).get("wall_time_s")
    busy = (profile.get("totals") or {}).get("gpu_busy_s")
    host_gap = (isinstance(duration, (int, float)) and duration > 0 and
                isinstance(busy, (int, float)) and 0 <= busy < duration * .7)
    return ["dominant-kernel" if fraction >= .5 else "distributed-cost",
            "gpu-idle-visible" if host_gap else "host-contribution-unresolved",
            "short-roi" if isinstance(duration, (int, float)) and duration < .1 else "long-or-unknown-roi"]


def store_path(plan, bound):
    # Tests may redirect the cache; normal runs share experience in this checkout.
    settings = plan["profiling"]["skill_runtime"]
    root = Path(settings.get("cache_dir") or REPO / "runs/_cache/skill-runtime")
    return root / (bound["key"] + ".json")


def recipe_key(candidate, needs, nature):
    return digest({"provider": candidate.get("skill") or candidate.get("provider"),
                   "mode": candidate.get("tool"), "needs": sorted(needs), "nature": nature})

