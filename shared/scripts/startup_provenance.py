"""Record reproducibility facts and optionally compare complete declared source.

This records optimizer-visible headers as well as compiled translation units. It
does not delete, restore, select, or modify any application source.
"""

import os
from fnmatch import fnmatch
import subprocess
from pathlib import Path

from experiment_support import source_files, sha256, write


def inventory(root: Path, patterns: list[str], ignored: list[str]) -> list[dict]:
    rows = []
    for path in source_files(root, patterns):
        relative = path.relative_to(root)
        if any(fnmatch(part, pattern) for part in relative.parts for pattern in ignored):
            continue
        rows.append({"path": str(relative), "bytes": path.stat().st_size, "sha256": sha256(path)})
    return rows


def source_comparison(source: Path, reference: Path, patterns: list[str], ignored=None) -> dict:
    before = {r["path"]: r["sha256"] for r in inventory(reference, patterns, ignored or [])}
    after = {r["path"]: r["sha256"] for r in inventory(source, patterns, ignored or [])}
    return {"reference": str(reference), "same": before == after,
            "missing": sorted(before.keys() - after.keys()),
            "added": sorted(after.keys() - before.keys()),
            "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


def probe(argv: list[str]) -> dict:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        return {"command": argv, "returncode": result.returncode,
                "output": (result.stdout or result.stderr).strip()[:1600]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": argv, "returncode": None, "output": None,
                "unavailable": type(exc).__name__}


def record(run: Path, baseline: Path, plan: dict, found: dict, facts: dict) -> None:
    env = plan.get("env") or {}
    cache = env.get("CMAKE_CUDA_COMPILER_LAUNCHER") if env.get("CCACHE_DIR") == "{run_dir}/compiler-cache" else None
    document = {"protocol": "profileweaver.startup-provenance/v1",
                "scope": "observed-startup-not-proof-of-identical-runtime-conditions",
                "source_inventory": inventory(baseline, plan["source"]["fingerprint_globs"],
                                              plan["source"].get("copy_ignore", [])),
                "host_cxx_substitution": probe([facts["host_cxx"], "--version"]),
                "tools": found | ({"ccache": cache} if cache else {}),
                "gpu_inventory": probe([found["nvidia-smi"],
                    "--query-gpu=uuid,name,driver_version,compute_cap", "--format=csv,noheader"]),
                "inherited_environment": {key: os.environ.get(key) for key in (
                    "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "CUDAHOSTCXX", "CUDACXX",
                    "CC", "CXX", "OMP_NUM_THREADS", "PW_OPTIMIZER_MODEL", "PW_OPTIMIZER_EFFORT")},
                "declared_environment": {"plan": plan.get("env", {}),
                                          "execution": (plan.get("execution") or {}).get("env", {})},
                "resolved_build": (plan.get("execution") or {}).get("build") or plan.get("build"),
                "unknowns": ["GPU clock and competing workload identity", "optimizer identity unless supplied"]}
    path = run / "startup-provenance.json"
    # Preserve the first observation if a resumed start uses a different shell.
    write(path.with_name("startup-provenance.latest.json") if path.exists() else path, document)
