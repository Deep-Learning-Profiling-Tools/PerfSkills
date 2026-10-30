"""Fingerprint and classify cached preflight checks."""

import hashlib
import json
import os
import re
import shutil
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


def expand(value, variables):
    return value.format_map(variables)


def resolve(value, variables):
    path = Path(expand(value, variables))
    return path if path.is_absolute() else REPO / path


def file_state(path):
    if not path.exists():
        return {"path": str(path), "state": "missing"}
    stat = path.stat()
    if path.is_file():
        return {"path": str(path), "state": "file", "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns}
    rows = []
    for item in sorted(x for x in path.rglob("*") if x.is_file()
                       and "__pycache__" not in x.parts):
        item_stat = item.stat()
        rows.append({"path": str(item.relative_to(path)), "bytes": item_stat.st_size,
                     "mtime_ns": item_stat.st_mtime_ns})
    return {"path": str(path), "state": "directory", "files": rows}


def executable_state(name):
    found = name if Path(name).is_absolute() else shutil.which(name)
    if not found:
        return {"name": name, "state": "missing"}
    path = Path(found).resolve()
    stat = path.stat()
    return {"name": name, "state": "present", "path": str(path), "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def contract(plan, variables):
    tools = sorted(set(plan.get("required_tools", [])) |
                   {check["argv"][0] for check in plan["checks"]})
    material = {
        "plan": plan,
        "tools": [executable_state(expand(tool, variables)) for tool in tools],
        "environment": {name: os.environ.get(name) for name in plan.get("fingerprint_env", [])},
        "inputs": [file_state(resolve(path, variables))
                   for path in plan.get("fingerprint_inputs", [])],
        "check_inputs": {check["id"]: [file_state(resolve(path, variables))
                         for path in check.get("fingerprint_inputs", [])]
                         for check in plan["checks"]},
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), material["tools"]


def select(source, stdout, stderr):
    return {"stdout": stdout, "stderr": stderr,
            "combined": stdout + "\n" + stderr}[source]


def classify(text, returncode):
    rules = [
        ("missing-shared-library", r"error while loading shared libraries:\s*([^:\s]+)"),
        ("compiler-target-unsupported", r"unsupported (?:gpu )?architecture|unsupported.*compute capability"),
        ("runtime-target-mismatch", r"no kernel image is available|invalid device function"),
        ("compiler-error", r"(?:fatal error|error):"),
        ("profiler-permission", r"permission|ERR_NVGPUCTRPERM"),
        ("driver-runtime-mismatch", r"driver version is insufficient|failed call to cuInit"),
        ("out-of-memory", r"out of memory|CUDA_ERROR_OUT_OF_MEMORY"),
        ("missing-command", r"No such file or directory|command not found"),
    ]
    for label, pattern in rules:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return {"class": label, "detail": match.group(0)[:240]}
    return {"class": "nonzero-returncode" if returncode else "expectation-mismatch",
            "detail": f"returncode={returncode}"}


def outputs_state(check, variables):
    return [file_state(resolve(path, variables)) for path in check.get("produces", [])]


def cache_valid(prior, contract_hash, plan, variables):
    if prior.get("status") != "complete" or prior.get("contract_sha256") != contract_hash:
        return False
    expected = [state for check in plan["checks"]
                for state in outputs_state(check, variables)]
    return expected == prior.get("produced", [])
