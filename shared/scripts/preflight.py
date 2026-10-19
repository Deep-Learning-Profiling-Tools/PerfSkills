#!/usr/bin/env python3
"""Run and cache compiler, device, profiler, build, launch, and oracle checks off-channel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
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
    for item in sorted(x for x in path.rglob("*") if x.is_file() and "__pycache__" not in x.parts):
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
        "inputs": [file_state(resolve(path, variables)) for path in plan.get("fingerprint_inputs", [])],
        "check_inputs": {check["id"]: [file_state(resolve(path, variables))
                         for path in check.get("fingerprint_inputs", [])] for check in plan["checks"]},
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, material["tools"]


def select(source, stdout, stderr):
    return {"stdout": stdout, "stderr": stderr, "combined": stdout + "\n" + stderr}[source]


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
    expected = [state for check in plan["checks"] for state in outputs_state(check, variables)]
    return expected == prior.get("produced", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    plan = load(args.plan.resolve(), {})
    if plan.get("version") != 1 or not plan.get("checks"):
        parser.error("preflight plan must be JSON with version=1 and at least one check")
    variables = {"repo": str(REPO), "run_id": plan["run_id"]}
    run_dir = resolve(plan["run_dir"], variables)
    run_dir.mkdir(parents=True, exist_ok=True)
    variables["run_dir"] = str(run_dir)
    result_path = run_dir / "preflight-result.json"
    try:
        contract_hash, tools = contract(plan, variables)
    except Exception as exc:
        raw = run_dir / "raw/preflight/contract.stderr.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"{type(exc).__name__}: {exc}\n")
        print(json.dumps({"status": "failed", "run_id": plan.get("run_id"),
                          "failed_check": "contract", "diagnostic": "invalid-contract-input",
                          "raw": str(raw)}, separators=(",", ":")))
        return 1
    prior = load(result_path, {})
    if cache_valid(prior, contract_hash, plan, variables):
        print(json.dumps({"status": "cached", "run_id": plan["run_id"],
                          "contract": contract_hash[:12], "result": str(result_path),
                          "brief": str(run_dir / "preflight-brief.txt")}, separators=(",", ":")))
        return 0

    missing = [item["name"] for item in tools if item["state"] == "missing"]
    if missing:
        result = {"protocol": "profileweaver.preflight-result/v1", "status": "failed",
                  "run_id": plan["run_id"], "contract_sha256": contract_hash,
                  "generated_utc": datetime.now(timezone.utc).isoformat(), "tools": tools,
                  "checks": [], "failed_check": "required-tools", "produced": [],
                  "diagnostic": {"class": "missing-command", "missing": missing}}
        write(result_path, result)
        brief_path = run_dir / "preflight-brief.txt"
        brief_path.write_text(
            f"PW3 preflight failed; contract={contract_hash[:12]}; checks=0/{len(plan['checks'])}; "
            f"failure=required-tools:missing-command; missing={','.join(missing)}\n")
        print(json.dumps({"status": "failed", "run_id": plan["run_id"],
                          "contract": contract_hash[:12], "checks_passed": 0,
                          "checks_total": len(plan["checks"]), "failed_check": "required-tools",
                          "result": str(result_path), "brief": str(brief_path)}, separators=(",", ":")))
        return 1

    base_env = os.environ.copy() | {key: expand(value, variables) for key, value in plan.get("env", {}).items()}
    rows, completed, failed = [], set(), None
    for check in plan["checks"]:
        if any(dep not in completed for dep in check.get("requires", [])):
            row = {"id": check["id"], "kind": check.get("kind", "other"), "status": "blocked",
                   "reason": "dependency-not-complete"}
            rows.append(row); failed = failed or check["id"]
            continue
        argv = [expand(value, variables) for value in check["argv"]]
        cwd = resolve(check.get("cwd", str(REPO)), variables)
        env = base_env | {key: expand(value, variables) for key, value in check.get("env", {}).items()}
        stem = run_dir / "raw/preflight" / check["id"]
        stem.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                  timeout=check.get("timeout_s"))
            stdout = proc.stdout.decode("utf-8", "replace")
            stderr = proc.stderr.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            proc = None; stdout = ""; stderr = str(exc)
        stem.with_suffix(".stdout.txt").write_text(stdout)
        stem.with_suffix(".stderr.txt").write_text(stderr)
        expect = check.get("expect") or {}
        expected_rc = expect.get("returncode", 0)
        rc = proc.returncode if proc else 127
        passed = rc == expected_rc
        if expect.get("regex"):
            passed = passed and bool(re.search(expect["regex"],
                                               select(expect.get("source", "combined"), stdout, stderr),
                                               re.MULTILINE))
        extracted = {}
        for spec in check.get("extract", []):
            match = re.search(spec["regex"], select(spec.get("source", "combined"), stdout, stderr),
                              re.MULTILINE)
            extracted[spec["name"]] = match.group(spec.get("group", 1)) if match else None
        row = {"id": check["id"], "kind": check.get("kind", "other"),
               "status": "passed" if passed else "failed", "returncode": rc,
               "extracted": extracted, "stdout": str(stem.with_suffix(".stdout.txt")),
               "stderr": str(stem.with_suffix(".stderr.txt")),
               "produced": outputs_state(check, variables)}
        if not passed:
            row["diagnostic"] = classify(stdout + "\n" + stderr, rc)
            if not check.get("allow_failure", False):
                failed = check["id"]
        else:
            completed.add(check["id"])
        rows.append(row)
        if failed:
            break
    produced = [state for row in rows for state in row.get("produced", [])]
    status = "failed" if failed else "complete"
    result = {"protocol": "profileweaver.preflight-result/v1", "status": status,
              "run_id": plan["run_id"], "contract_sha256": contract_hash,
              "generated_utc": datetime.now(timezone.utc).isoformat(), "tools": tools,
              "checks": rows, "failed_check": failed, "produced": produced}
    write(result_path, result)
    facts = [f"{key}={value}" for row in rows for key, value in row.get("extracted", {}).items()
             if value is not None]
    if failed:
        row = next(item for item in rows if item["id"] == failed)
        tail = f" failure={failed}:{row['diagnostic']['class']}; raw={row['stderr']}"
    else:
        tail = ""
    brief = (f"PW3 preflight {status}; contract={contract_hash[:12]}; "
             f"checks={sum(x['status']=='passed' for x in rows)}/{len(plan['checks'])}; "
             f"facts={','.join(facts) or 'none'};{tail}\n")
    (run_dir / "preflight-brief.txt").write_text(brief)
    print(json.dumps({"status": status, "run_id": plan["run_id"],
                      "contract": contract_hash[:12], "checks_passed": sum(x["status"] == "passed" for x in rows),
                      "checks_total": len(plan["checks"]), "failed_check": failed,
                      "result": str(result_path), "brief": str(run_dir / "preflight-brief.txt")},
                     separators=(",", ":")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
