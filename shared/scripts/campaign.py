#!/usr/bin/env python3
"""Run a workload-agnostic, paired, adaptive ProfileWeaver optimization campaign.

The agent supplies commands and extraction rules in one JSON plan.  This runner owns builds,
sampling, correctness checks, statistics, caching, finalist confirmation, raw log capture, and
compact summaries.  Application stdout/stderr never reaches the terminal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics as st
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SEED = 20261011


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


def quantile(values, fraction):
    return values[min(len(values) - 1, max(0, int(fraction * len(values))))]


def summarize(values):
    median = st.median(values)
    return {"samples": values, "median": median,
            "mad": st.median(abs(value - median) for value in values),
            "min": min(values), "max": max(values)}


def improvement(base, candidate, direction, iterations, seed):
    """Paired bootstrap CI for the ratio of medians in the improvement direction."""
    if len(base) != len(candidate) or len(base) < 2:
        return None
    rng = random.Random(seed + len(base))
    ratios = []
    for _ in range(iterations):
        indices = rng.choices(range(len(base)), k=len(base))
        b = st.median(base[index] for index in indices)
        c = st.median(candidate[index] for index in indices)
        ratios.append((b / c) if direction == "lower" else (c / b))
    ratios.sort()
    b, c = st.median(base), st.median(candidate)
    return {"point": (b / c) if direction == "lower" else (c / b),
            "ci95": [quantile(ratios, .025), quantile(ratios, .975)]}


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


def run_command(item, label, sample_index, plan, run_dir, variables):
    item_vars = variables | {"id": item["id"], "sample": str(sample_index), "label": label}
    cwd = resolve(item.get("cwd", str(REPO)), item_vars)
    argv = [expand(x, item_vars) for x in item["command"]]
    env = os.environ.copy() | {key: expand(value, item_vars) for key, value in plan.get("env", {}).items()}
    env |= {key: expand(value, item_vars) for key, value in item.get("env", {}).items()}
    env |= {"PW_CAMPAIGN_ID": item["id"], "PW_SAMPLE_INDEX": str(sample_index)}
    stem = run_dir / "raw/campaign" / label
    stem.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                          timeout=item.get("timeout_s", plan.get("timeout_s")))
    wall_s = time.perf_counter() - start
    stem.with_suffix(".stdout.txt").write_bytes(proc.stdout)
    stem.with_suffix(".stderr.txt").write_bytes(proc.stderr)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    metric = extract(plan["metric"], stdout, stderr, wall_s, item_vars)
    oracle = check_oracle(plan.get("oracle"), proc.returncode, stdout, stderr, cwd, env,
                          item_vars, stem)
    return {"metric": metric, "wall_s": wall_s, "oracle": oracle,
            "stdout": str(stem.with_suffix(".stdout.txt")),
            "stderr": str(stem.with_suffix(".stderr.txt"))}


def run_build(item, plan, run_dir, variables):
    if not item.get("build"):
        return None
    item_vars = variables | {"id": item["id"]}
    cwd = resolve(item.get("build_cwd", item.get("cwd", str(REPO))), item_vars)
    argv = [expand(x, item_vars) for x in item["build"]]
    env = os.environ.copy() | {key: expand(value, item_vars) for key, value in plan.get("env", {}).items()}
    stem = run_dir / "raw/campaign/build" / item["id"]
    stem.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                          timeout=item.get("build_timeout_s", plan.get("timeout_s")))
    stem.with_suffix(".stdout.txt").write_bytes(proc.stdout)
    stem.with_suffix(".stderr.txt").write_bytes(proc.stderr)
    if proc.returncode:
        raise RuntimeError(f"build {item['id']} returned {proc.returncode}; see {stem}.stderr.txt")
    return {"returncode": 0, "stdout": str(stem.with_suffix(".stdout.txt")),
            "stderr": str(stem.with_suffix(".stderr.txt"))}


def run_pair(base, candidate, pair, plan, run_dir, variables, prefix):
    rows = {}
    order = [base, candidate] if pair % 2 == 0 else [candidate, base]
    for item in order:
        rows[item["id"]] = run_command(item, f"{prefix}-{pair:02d}-{item['id']}", pair,
                                        plan, run_dir, variables)
    return rows[base["id"]], rows[candidate["id"]]


def decision(base_values, candidate_values, direction, sampling, seed):
    result = improvement(base_values, candidate_values, direction,
                         sampling.get("bootstrap_iterations", 2000), seed)
    threshold = sampling.get("practical_threshold", 1.02)
    low, high = result["ci95"]
    if low >= threshold:
        reason = "decisive-improvement"
    elif high <= 1 / threshold:
        reason = "decisive-regression"
    elif low >= 1 / threshold and high <= threshold:
        reason = "practical-equivalence"
    else:
        reason = None
    return result, reason


def evaluate_candidate(base, candidate, plan, run_dir, variables, number):
    sampling = plan["sampling"]
    for warmup in range(sampling.get("warmup", 1)):
        run_pair(base, candidate, -warmup - 1, plan, run_dir, variables,
                 f"candidate-{candidate['id']}-warmup")
    base_rows, candidate_rows = [], []
    stop = "maximum-pairs"
    for pair in range(sampling.get("max_pairs", 9)):
        left, right = run_pair(base, candidate, pair, plan, run_dir, variables,
                               f"candidate-{candidate['id']}")
        base_rows.append(left); candidate_rows.append(right)
        if not right["oracle"]["passed"]:
            stop = "correctness-failed"
            break
        if len(candidate_rows) >= sampling.get("min_pairs", 5):
            comparison, reason = decision([x["metric"] for x in base_rows],
                                          [x["metric"] for x in candidate_rows],
                                          plan["metric"].get("direction", "lower"),
                                          sampling, SEED + number * 100)
            if reason:
                stop = reason
                break
    comparison = improvement([x["metric"] for x in base_rows],
                              [x["metric"] for x in candidate_rows],
                              plan["metric"].get("direction", "lower"),
                              sampling.get("bootstrap_iterations", 2000), SEED + number * 100)
    wall = improvement([x["wall_s"] for x in base_rows], [x["wall_s"] for x in candidate_rows],
                       "lower", sampling.get("bootstrap_iterations", 2000), SEED + number * 100 + 1)
    return {"id": candidate["id"], "description": candidate.get("description"),
            "mechanism": candidate.get("mechanism"), "pairs": len(candidate_rows), "stop": stop,
            "correctness": {"passed": all(x["oracle"]["passed"] for x in candidate_rows)},
            "metric": summarize([x["metric"] for x in candidate_rows]),
            "baseline_metric": summarize([x["metric"] for x in base_rows]),
            "improvement": comparison, "wall": summarize([x["wall_s"] for x in candidate_rows]),
            "baseline_wall": summarize([x["wall_s"] for x in base_rows]),
            "wall_improvement": wall, "samples": {"baseline": base_rows, "candidate": candidate_rows}}


def final_confirmation(base, candidates, initial, plan, run_dir, variables):
    sampling = plan["sampling"]
    eligible = [item for item in initial
                if item["correctness"]["passed"] and item.get("improvement")]
    ranked = sorted(eligible,
                    key=lambda x: (x.get("improvement") or {}).get("point", 0), reverse=True)
    if not ranked:
        raise RuntimeError("no correct candidate reached final confirmation")
    finalists = ranked[:min(len(ranked), sampling.get("finalists", 2))]
    by_id = {item["id"]: item for item in candidates}
    chosen = [by_id[item["id"]] for item in finalists]
    rows = {base["id"]: []} | {item["id"]: [] for item in chosen}
    stop = "maximum-pairs"
    gates = {}
    for pair in range(sampling.get("final_max_pairs", 15)):
        order = [base, *chosen]
        if pair % 2:
            order = list(reversed(order))
        for item in order:
            rows[item["id"]].append(run_command(
                item, f"final-{pair:02d}-{item['id']}", pair, plan, run_dir, variables))
        if pair + 1 < sampling.get("final_min_pairs", 5):
            continue
        direction = plan["metric"].get("direction", "lower")
        winner = chosen[0]
        base_cmp = improvement([x["metric"] for x in rows[base["id"]]],
                               [x["metric"] for x in rows[winner["id"]]], direction,
                               sampling.get("bootstrap_iterations", 2000), SEED + 700)
        wall_cmp = improvement([x["wall_s"] for x in rows[base["id"]]],
                               [x["wall_s"] for x in rows[winner["id"]]], "lower",
                               sampling.get("bootstrap_iterations", 2000), SEED + 701)
        separation = None
        if len(chosen) > 1:
            runner = chosen[1]
            separation = improvement([x["metric"] for x in rows[runner["id"]]],
                                     [x["metric"] for x in rows[winner["id"]]], direction,
                                     sampling.get("bootstrap_iterations", 2000), SEED + 702)
        threshold = sampling.get("practical_threshold", 1.02)
        gates = {
            "correctness": all(row["oracle"]["passed"] for values in rows.values() for row in values),
            "practical_improvement": base_cmp["ci95"][0] >= threshold,
            "winner_separated": separation is None or separation["ci95"][0] > 1.0,
            "no_wall_regression_over_threshold": wall_cmp["ci95"][0] > 1 / threshold,
        }
        if all(gates.values()):
            stop = "all-gates-resolved"
            break
    winner = chosen[0]
    result = {
        "winner": winner["id"], "runner_up": chosen[1]["id"] if len(chosen) > 1 else None,
        "pairs": len(rows[base["id"]]), "stop": stop, "gates": gates,
        "baseline_vs_winner": improvement([x["metric"] for x in rows[base["id"]]],
                                          [x["metric"] for x in rows[winner["id"]]],
                                          plan["metric"].get("direction", "lower"),
                                          sampling.get("bootstrap_iterations", 2000), SEED + 700),
        "wall_baseline_vs_winner": improvement([x["wall_s"] for x in rows[base["id"]]],
                                               [x["wall_s"] for x in rows[winner["id"]]], "lower",
                                               sampling.get("bootstrap_iterations", 2000), SEED + 701),
        "samples": rows,
    }
    if len(chosen) > 1:
        result["runner_up_vs_winner"] = improvement(
            [x["metric"] for x in rows[chosen[1]["id"]]],
            [x["metric"] for x in rows[winner["id"]]],
            plan["metric"].get("direction", "lower"),
            sampling.get("bootstrap_iterations", 2000), SEED + 702)
    return result


def screening_decision(results, plan):
    """Select the best screened candidate without spending finalist-confirmation runs."""
    eligible = [item for item in results
                if item["correctness"]["passed"] and item.get("improvement")]
    ranked = sorted(eligible,
                    key=lambda item: item["improvement"]["point"], reverse=True)
    if not ranked:
        raise RuntimeError("no correct candidate completed screening")
    winner = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    threshold = plan["sampling"].get("practical_threshold", 1.02)
    return {
        "winner": winner["id"], "runner_up": runner["id"] if runner else None,
        "pairs": winner["pairs"], "stop": "screen-complete",
        "gates": {
            "correctness": winner["correctness"]["passed"],
            "practical_improvement": winner["improvement"]["ci95"][0] >= threshold,
        },
        "baseline_vs_winner": winner["improvement"],
        "wall_baseline_vs_winner": winner["wall_improvement"],
        "samples": winner["samples"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    plan = load(args.plan.resolve())
    if not plan or plan.get("version") != 1:
        parser.error("campaign plan must be JSON with version=1")
    validate_plan(plan)
    variables = {"repo": str(REPO), "run_id": plan["run_id"]}
    run_dir = resolve(plan["run_dir"], variables)
    run_dir.mkdir(parents=True, exist_ok=True)
    variables["run_dir"] = str(run_dir)
    fingerprint = campaign_fingerprint(plan, variables)
    result_path = run_dir / "campaign-result.json"
    prior = load(result_path, {})
    if prior.get("status") == "complete" and prior.get("fingerprint") == fingerprint:
        print(json.dumps({"status": "cached", "run_id": plan["run_id"],
                          "winner": (prior.get("final") or {}).get("winner"),
                          "result": str(result_path), "brief": str(run_dir / "campaign-brief.txt")},
                         separators=(",", ":")))
        return 0
    started = time.perf_counter()
    baseline, candidates = plan["baseline"], plan["candidates"]
    try:
        phase = plan.get("phase", "confirm")
        builds = {baseline["id"]: run_build(baseline, plan, run_dir, variables)}
        results = []
        for index, candidate in enumerate(candidates, 1):
            builds[candidate["id"]] = run_build(candidate, plan, run_dir, variables)
            if phase != "final-only":
                results.append(evaluate_candidate(baseline, candidate, plan, run_dir, variables, index))
        if phase == "final-only":
            prior = {item["id"]: item for item in plan["screening"]}
            results = [prior[item["id"]] for item in candidates]
        final = (screening_decision(results, plan) if phase == "screen" else
                 final_confirmation(baseline, candidates, results, plan, run_dir, variables))
        fingerprint_after = campaign_fingerprint(plan, variables)
        if fingerprint_after != fingerprint:
            raise RuntimeError("declared campaign inputs changed during execution")
        result = {"protocol": "profileweaver.campaign-result/v1", "status": "complete",
                  "run_id": plan["run_id"], "fingerprint": fingerprint,
                  "fingerprint_after": fingerprint_after,
                  "generated_utc": datetime.now(timezone.utc).isoformat(),
                  "elapsed_s": time.perf_counter() - started, "phase": phase,
                  "metric": plan["metric"],
                  "sampling": plan["sampling"], "builds": builds, "candidates": results,
                  "final": final}
        write(result_path, result)
        iterations = []
        for index, item in enumerate(results, 1):
            primary = {"median": item["metric"]["median"], "unit": plan["metric"].get("unit"),
                       "name": plan["metric"].get("name"),
                       "improvement": (item.get("improvement") or {}).get("point")}
            entry = {"iteration": index, "optimization": item.get("description") or item["id"],
                     "mechanism": item.get("mechanism"),
                     "result": {"primary": primary, "correctness": item["correctness"]}}
            if plan["metric"].get("unit") in {"s", "sec", "second", "seconds"}:
                entry["result"]["roi"] = {"median_s": primary["median"],
                                            "speedup": primary["improvement"]}
            iterations.append(entry)
        write(run_dir / "iterations.json", iterations)
        winner = next(item for item in results if item["id"] == final["winner"])
        brief = (f"PW4 campaign {phase}; candidates={len(results)}; winner={final['winner']}; "
                 f"improvement={final['baseline_vs_winner']['point']:.3f}x "
                 f"CI=[{final['baseline_vs_winner']['ci95'][0]:.3f},{final['baseline_vs_winner']['ci95'][1]:.3f}]; "
                 f"pairs={final['pairs']} stop={final['stop']}; correctness="
                 f"{'PASS' if final['gates'].get('correctness') else 'FAIL'}; "
                 f"mechanism={winner.get('mechanism')}; full={result_path}\n")
        (run_dir / "campaign-brief.txt").write_text(brief)
        print(json.dumps({"status": "complete", "run_id": plan["run_id"],
                          "phase": phase, "winner": final["winner"],
                          "improvement": final["baseline_vs_winner"]["point"],
                          "pairs": final["pairs"], "stop": final["stop"],
                          "result": str(result_path), "brief": str(run_dir / "campaign-brief.txt")},
                         separators=(",", ":")))
        return 0
    except Exception as exc:
        result = {"protocol": "profileweaver.campaign-result/v1", "status": "failed",
                  "run_id": plan["run_id"], "fingerprint": fingerprint,
                  "elapsed_s": time.perf_counter() - started, "error": str(exc)}
        write(result_path, result)
        print(json.dumps({"status": "failed", "run_id": plan["run_id"],
                          "error": str(exc), "result": str(result_path)}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
