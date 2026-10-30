#!/usr/bin/env python3
"""Run and cache compiler, device, profiler, build, launch, and oracle checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from preflight_support import (REPO, cache_valid, classify, contract, expand, load,
                               outputs_state, resolve, select, write)


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
            f"ProfileWeaver preflight failed; contract={contract_hash[:12]}; "
            f"checks=0/{len(plan['checks'])}; failure=required-tools:missing-command; "
            f"missing={','.join(missing)}\n")
        print(json.dumps({"status": "failed", "run_id": plan["run_id"],
                          "contract": contract_hash[:12], "checks_passed": 0,
                          "checks_total": len(plan["checks"]), "failed_check": "required-tools",
                          "result": str(result_path), "brief": str(brief_path)},
                         separators=(",", ":")))
        return 1
    base_env = os.environ.copy() | {
        key: expand(value, variables) for key, value in plan.get("env", {}).items()}
    rows, completed, failed = [], set(), None
    for check in plan["checks"]:
        if any(dep not in completed for dep in check.get("requires", [])):
            rows.append({"id": check["id"], "kind": check.get("kind", "other"),
                         "status": "blocked", "reason": "dependency-not-complete"})
            failed = failed or check["id"]
            continue
        argv = [expand(value, variables) for value in check["argv"]]
        cwd = resolve(check.get("cwd", str(REPO)), variables)
        env = base_env | {key: expand(value, variables)
                          for key, value in check.get("env", {}).items()}
        stem = run_dir / "raw/preflight" / check["id"]
        stem.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                  timeout=check.get("timeout_s"))
            stdout = proc.stdout.decode("utf-8", "replace")
            stderr = proc.stderr.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            proc, stdout, stderr = None, "", str(exc)
        stem.with_suffix(".stdout.txt").write_text(stdout)
        stem.with_suffix(".stderr.txt").write_text(stderr)
        expect = check.get("expect") or {}
        rc = proc.returncode if proc else 127
        passed = rc == expect.get("returncode", 0)
        if expect.get("regex"):
            passed &= bool(re.search(expect["regex"],
                           select(expect.get("source", "combined"), stdout, stderr),
                           re.MULTILINE))
        extracted = {}
        for spec in check.get("extract", []):
            match = re.search(spec["regex"],
                              select(spec.get("source", "combined"), stdout, stderr),
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
    facts = [f"{key}={value}" for row in rows
             for key, value in row.get("extracted", {}).items() if value is not None]
    if failed:
        row = next(item for item in rows if item["id"] == failed)
        tail = f" failure={failed}:{row['diagnostic']['class']}; raw={row['stderr']}"
    else:
        tail = ""
    brief = (f"ProfileWeaver preflight {status}; contract={contract_hash[:12]}; "
             f"checks={sum(x['status']=='passed' for x in rows)}/{len(plan['checks'])}; "
             f"facts={','.join(facts) or 'none'};{tail}\n")
    brief_path = run_dir / "preflight-brief.txt"
    brief_path.write_text(brief)
    print(json.dumps({"status": status, "run_id": plan["run_id"],
                      "contract": contract_hash[:12],
                      "checks_passed": sum(x["status"] == "passed" for x in rows),
                      "checks_total": len(plan["checks"]), "failed_check": failed,
                      "result": str(result_path), "brief": str(brief_path)},
                     separators=(",", ":")))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
