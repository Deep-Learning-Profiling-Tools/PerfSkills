#!/usr/bin/env python3
"""Execute a ProfileWeaver plan with stage caching and bounded terminal output.

Commands never use an implicit shell. Full stdout/stderr go under raw/harness; the terminal
receives one small status object per stage and one final brief status object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_view import build_view, load as load_json, select_variant_profile
from brief import build as build_brief


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-stage")
    parser.add_argument("--until-stage")
    parser.add_argument("--max-agent-bytes", type=int, default=4096)
    parser.add_argument("--max-agent-tokens", type=int, default=320)
    parser.add_argument("--learn", action="store_true", help="ingest a completed report into the outcome ledger")
    args = parser.parse_args()

    plan_path = args.plan.resolve()
    plan = load(plan_path)
    if not isinstance(plan, dict) or plan.get("version") != 1:
        emit(status="invalid-plan", detail="plan must be a JSON object with version=1")
        return 2
    run_id = plan.get("run_id")
    variables = {"repo": str(REPO), "run_id": run_id, "run_dir": plan.get("run_dir", f"runs/{run_id}")}
    run = resolve(plan["run_dir"], variables)
    variables["run_dir"] = str(run)
    raw = run / "raw" / "harness"
    raw.mkdir(parents=True, exist_ok=True)
    state_path = run / "harness-state.json"
    state = load(state_path, {"protocol": "profileweaver.harness-state/v1", "run_id": run_id, "stages": {}})
    state.setdefault("stages", {})
    base_env = {k: expand(v, variables) for k, v in plan.get("env", {}).items()}
    global_fingerprint_env = plan.get("fingerprint_env", [])
    stages = plan.get("stages") or []
    ids = [stage.get("id") for stage in stages]
    if any(not x for x in ids) or len(ids) != len(set(ids)):
        emit(status="invalid-plan", detail="stage ids must be present and unique")
        return 2
    if args.from_stage and args.from_stage not in ids:
        emit(status="invalid-plan", detail=f"unknown --from-stage {args.from_stage}")
        return 2
    if args.until_stage and args.until_stage not in ids:
        emit(status="invalid-plan", detail=f"unknown --until-stage {args.until_stage}")
        return 2

    active = args.from_stage is None
    overall = "complete"
    completed = {sid for sid, value in state["stages"].items()
                 if value.get("status") in {"success", "cached"}}
    for stage in stages:
        sid = stage["id"]
        if sid == args.from_stage:
            active = True
        if not active:
            continue
        unmet = [x for x in stage.get("requires", []) if x not in completed]
        if unmet:
            emit(stage=sid, status="blocked", unmet=unmet)
            overall = "blocked"
            break
        fingerprint = stage_fingerprint(stage, variables, base_env, global_fingerprint_env)
        prior = state["stages"].get(sid, {})
        if stage.get("cache", True) and prior.get("fingerprint") == fingerprint and outputs_match(prior.get("outputs"), stage, variables):
            prior["status"] = "cached"
            state["stages"][sid] = prior
            emit(stage=sid, status="cached", elapsed_s=0)
            completed.add(sid)
        elif args.dry_run:
            emit(stage=sid, status="would-run", argv=[expand(x, variables) for x in stage["argv"]])
            completed.add(sid)
        else:
            argv = [expand(x, variables) for x in stage["argv"]]
            cwd = resolve(stage.get("cwd", str(REPO)), variables)
            env = os.environ.copy()
            env.update(base_env)
            env.update({k: expand(v, variables) for k, v in stage.get("env", {}).items()})
            stdout_path, stderr_path = raw / f"{sid}.stdout.txt", raw / f"{sid}.stderr.txt"
            started = time.perf_counter()
            timed_out = False
            error = None
            try:
                with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                    proc = subprocess.run(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                          timeout=stage.get("timeout_s"), shell=False)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                returncode, timed_out = 124, True
            except OSError as exc:
                returncode, error = 127, str(exc)
            elapsed = time.perf_counter() - started
            produced = output_state(stage, variables)
            ok = returncode == 0 and all(x["state"] != "missing" for x in produced)
            record = {
                "status": "success" if ok else "failed", "fingerprint": fingerprint,
                "returncode": returncode, "timed_out": timed_out, "elapsed_s": elapsed,
                "error": error,
                "outputs": produced, "stdout": str(stdout_path.relative_to(run)),
                "stderr": str(stderr_path.relative_to(run)),
                "finished_utc": datetime.now(timezone.utc).isoformat(),
            }
            state["stages"][sid] = record
            write_json(state_path, state)
            emit(stage=sid, status=record["status"], returncode=returncode,
                 elapsed_s=round(elapsed, 3), outputs=len(produced))
            if not ok and not stage.get("allow_failure", False):
                overall = "failed"
                break
            if ok:
                completed.add(sid)
        write_json(state_path, state)
        if sid == args.until_stage:
            break

    if not args.dry_run:
        failed_stage = next((sid for sid, value in state["stages"].items()
                             if value.get("status") == "failed" and sid in ids), None)
        if overall == "complete":
            if not derive_profile_diff(run, state):
                overall = "failed"
                failed_stage = "_derive-profile-diff"
        harness_result = {"protocol": "profileweaver.harness-result/v1", "status": overall,
                          "failed_stage": failed_stage if overall != "complete" else None,
                          "timestamp_utc": datetime.now(timezone.utc).isoformat()}
        write_json(run / "harness-result.json", harness_result)
        write_json(state_path, state)
        agent_view_path, agent_view_error = None, None
        try:
            _, agent_view_path = build_view(run, args.max_agent_bytes)
        except Exception as exc:
            agent_view_error = str(exc)[:240]
        try:
            brief, brief_text = build_brief(run, "auto", args.max_agent_tokens)
            brief_json_path, brief_text_path = run / "agent-brief.json", run / "agent-brief.txt"
            write_json(brief_json_path, brief)
            temporary = brief_text_path.with_name(brief_text_path.name + ".tmp")
            temporary.write_text(brief_text)
            temporary.replace(brief_text_path)
            emit(status=overall, run_id=run_id, action=brief["action"],
                 agent_brief=str(brief_text_path.relative_to(REPO)),
                 tokens=brief["context"]["text_tokens"],
                 agent_view=str(agent_view_path.relative_to(REPO)) if agent_view_path else None,
                 agent_view_error=agent_view_error)
        except Exception as exc:
            emit(status=overall, run_id=run_id, agent_brief=None,
                 brief_error=str(exc)[:240],
                 agent_view=str(agent_view_path.relative_to(REPO)) if agent_view_path else None,
                 agent_view_error=agent_view_error)
        if args.learn and overall == "complete" and (run / "report.json").exists():
            learn = subprocess.run([sys.executable, str(REPO / "shared/scripts/learn.py"), "ingest", str(run)], cwd=REPO)
            if learn.returncode:
                return learn.returncode
    return 0 if overall == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
