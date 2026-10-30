"""Cached-stage utilities for the generic ProfileWeaver harness."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_view import load as load_json, select_variant_profile

REPO = Path(__file__).resolve().parents[2]

def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default

def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def expand(value, variables):
    return value.format_map(variables)

def resolve(value, variables):
    path = Path(expand(value, variables))
    return path if path.is_absolute() else REPO / path

def path_fingerprint(path):
    if not path.exists():
        return {"path": str(path), "state": "missing"}
    if path.is_file():
        return {"path": str(path), "state": "file", "bytes": path.stat().st_size, "sha256": sha256(path)}
    files = []
    for item in sorted(x for x in path.rglob("*") if x.is_file() and not x.is_symlink()):
        files.append({"path": str(item.relative_to(path)), "bytes": item.stat().st_size, "sha256": sha256(item)})
    return {"path": str(path), "state": "directory", "files": files}

def stage_fingerprint(stage, variables, base_env, global_fingerprint_env):
    env_names = sorted(set(global_fingerprint_env) | set(stage.get("fingerprint_env", [])))
    material = {
        "argv": [expand(x, variables) for x in stage["argv"]],
        "cwd": expand(stage.get("cwd", str(REPO)), variables),
        "env": {**base_env, **{k: expand(v, variables) for k, v in stage.get("env", {}).items()}},
        "ambient_env": {name: os.environ.get(name) for name in env_names},
        "inputs": [path_fingerprint(resolve(x, variables)) for x in stage.get("fingerprint_inputs", [])],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def output_state(stage, variables):
    return [path_fingerprint(resolve(x, variables)) for x in stage.get("produces", [])]

def outputs_match(old_outputs, stage, variables):
    current = output_state(stage, variables)
    return bool(current) and current == old_outputs and all(x["state"] != "missing" for x in current)

def emit(**fields):
    print(json.dumps(fields, separators=(",", ":")), flush=True)

def derive_profile_diff(run, state):
    """Create the impact-ranked delta automatically when both profiles exist."""
    baseline = run / "profile.baseline.json"
    report = load_json(run / "report.json") or {}
    variant = select_variant_profile(run, report)
    if not baseline.exists() or not variant:
        return True
    script = REPO / "shared/scripts/diff_profiles.py"
    sources, report_path = run / "source-targets.json", run / "report.json"
    inputs = [baseline, variant, script] + ([sources] if sources.exists() else []) + ([report_path] if report_path.exists() else [])
    fingerprint = hashlib.sha256("".join(sha256(path) for path in inputs).encode()).hexdigest()
    output = run / "profile-diff.json"
    prior = state["stages"].get("_derive-profile-diff", {})
    if prior.get("fingerprint") == fingerprint and output.exists() and prior.get("output_sha256") == sha256(output):
        emit(stage="_derive-profile-diff", status="cached", elapsed_s=0)
        return True
    argv = [sys.executable, str(script), "--baseline", str(baseline), "--variant", str(variant),
            "--output", str(output)]
    if sources.exists():
        argv += ["--source-targets", str(sources)]
    if report_path.exists():
        argv += ["--report", str(report_path)]
    raw = run / "raw" / "harness"
    stdout_path, stderr_path = raw / "_derive-profile-diff.stdout.txt", raw / "_derive-profile-diff.stderr.txt"
    started = time.perf_counter()
    if output.exists():
        output.unlink()  # derived data must never survive a failed re-derivation as if current
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.run(argv, cwd=REPO, stdout=stdout, stderr=stderr, shell=False)
    elapsed = time.perf_counter() - started
    if proc.returncode:
        state["stages"]["_derive-profile-diff"] = {
            "status": "failed", "fingerprint": fingerprint,
            "returncode": proc.returncode, "elapsed_s": elapsed,
            "stdout": str(stdout_path.relative_to(run)),
            "stderr": str(stderr_path.relative_to(run)),
        }
        emit(stage="_derive-profile-diff", status="failed", returncode=proc.returncode, elapsed_s=round(elapsed, 3))
        return False
    state["stages"]["_derive-profile-diff"] = {"status": "success", "fingerprint": fingerprint,
                                                 "output_sha256": sha256(output), "elapsed_s": elapsed}
    emit(stage="_derive-profile-diff", status="success", returncode=0, elapsed_s=round(elapsed, 3))
    return True
