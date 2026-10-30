#!/usr/bin/env python3
"""Campaign plans, fingerprints, extraction rules, and correctness oracles."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def validate_plan(plan):
    sampling = plan.get("sampling") or {}
    if sampling.get("min_pairs", 0) < 5 or sampling.get("final_min_pairs", 0) < 5:
        raise ValueError("candidate and final sampling require at least five pairs")
    if sampling["min_pairs"] > sampling["max_pairs"]:
        raise ValueError("min_pairs cannot exceed max_pairs")
    if sampling["final_min_pairs"] > sampling["final_max_pairs"]:
        raise ValueError("final_min_pairs cannot exceed final_max_pairs")
    if sampling.get("mode") == "winner-confirmation":
        low = sampling.get("screen_min_pairs", 1)
        high = sampling.get("screen_max_pairs", low)
        if low < 1 or low > high:
            raise ValueError("winner-confirmation screen pair limits are invalid")
    ids = [plan["baseline"]["id"], *(item["id"] for item in plan["candidates"])]
    if len(ids) != len(set(ids)):
        raise ValueError("baseline and candidate ids must be unique")
    if not plan["candidates"]:
        raise ValueError("at least one candidate is required")
    if plan.get("phase") == "final-only":
        screened = {item.get("id") for item in plan.get("screening", [])}
        missing = [item["id"] for item in plan["candidates"] if item["id"] not in screened]
        if missing:
            raise ValueError(f"final-only candidates lack screening summaries: {missing}")


def expand(value, variables):
    return value.format_map(variables)


def resolve(value, variables):
    path = Path(expand(value, variables))
    return path if path.is_absolute() else REPO / path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_state(path):
    if not path.exists():
        return {"path": str(path), "state": "missing"}
    if path.is_file():
        return {"path": str(path), "state": "file", "bytes": path.stat().st_size,
                "sha256": sha256(path)}
    rows = []
    for item in sorted(x for x in path.rglob("*") if x.is_file() and not x.is_symlink()):
        rows.append({"path": str(item.relative_to(path)), "bytes": item.stat().st_size,
                     "sha256": sha256(item)})
    return {"path": str(path), "state": "directory", "files": rows}


def campaign_fingerprint(plan, variables):
    inputs = []
    for raw in plan.get("fingerprint_inputs", []):
        inputs.append(path_state(resolve(raw, variables)))
    for item in [plan["baseline"], *plan["candidates"]]:
        for raw in item.get("fingerprint_inputs", []):
            inputs.append(path_state(resolve(raw, variables | {"id": item["id"]})))
    material = {"plan": plan, "inputs": inputs}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def extract(spec, stdout, stderr, wall_s, variables):
    source = spec.get("source", "wall")
    if source == "wall":
        return wall_s
    if source == "stdout":
        text = stdout
    elif source == "stderr":
        text = stderr
    elif source == "file":
        text = resolve(spec["path"], variables).read_text(errors="replace")
    else:
        raise ValueError(f"unsupported extraction source: {source}")
    match = re.search(spec["regex"], text, re.MULTILINE)
    if not match:
        raise ValueError(f"metric regex did not match {source}")
    group = spec.get("group", 1)
    value = match.group(group)
    return float(value) * float(spec.get("scale", 1.0))


def check_oracle(spec, returncode, stdout, stderr, cwd, env, variables, log_stem):
    if returncode != 0:
        return {"passed": False, "reason": f"command-returncode-{returncode}"}
    if not spec or spec.get("type", "returncode") == "returncode":
        return {"passed": True, "reason": "returncode-zero"}
    kind = spec["type"]
    if kind in {"stdout_regex", "stderr_regex", "file_regex"}:
        if kind == "stdout_regex":
            text = stdout
        elif kind == "stderr_regex":
            text = stderr
        else:
            text = resolve(spec["path"], variables).read_text(errors="replace")
        match = re.search(spec["regex"], text, re.MULTILINE)
        if not match:
            return {"passed": False, "reason": f"{kind}-missing"}
        if "expected" in spec:
            group = match.group(spec.get("group", 1))
            if str(group) != str(spec["expected"]):
                return {"passed": False, "reason": "oracle-value-mismatch"}
        return {"passed": True, "reason": kind}
    if kind == "command":
        argv = [expand(x, variables) for x in spec["argv"]]
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=spec.get("timeout_s"))
        log_stem.with_suffix(".oracle.stdout.txt").write_bytes(proc.stdout)
        log_stem.with_suffix(".oracle.stderr.txt").write_bytes(proc.stderr)
        return {"passed": proc.returncode == 0,
                "reason": "oracle-command-zero" if proc.returncode == 0 else f"oracle-returncode-{proc.returncode}"}
    raise ValueError(f"unsupported oracle type: {kind}")
