#!/usr/bin/env python3
"""Quietly build and execute paired baseline/candidate samples."""

from __future__ import annotations

import os
import subprocess
import time

from campaign_io import REPO, check_oracle, expand, extract, resolve
from campaign_stats import SEED, decision, improvement, summarize


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


def evaluate_candidate(base, candidate, plan, run_dir, variables, number):
    sampling = plan["sampling"]
    single_pass = (plan.get("phase") == "screen" and
                   sampling.get("mode") == "winner-confirmation")
    warmup_count = sampling.get("screen_warmup", 0) if single_pass else sampling.get("warmup", 1)
    min_pairs = sampling.get("screen_min_pairs", 1) if single_pass else sampling.get("min_pairs", 5)
    max_pairs = sampling.get("screen_max_pairs", 1) if single_pass else sampling.get("max_pairs", 9)
    for warmup in range(warmup_count):
        run_pair(base, candidate, -warmup - 1, plan, run_dir, variables,
                 f"candidate-{candidate['id']}-warmup")
    base_rows, candidate_rows = [], []
    stop = "maximum-pairs"
    for pair in range(max_pairs):
        left, right = run_pair(base, candidate, pair, plan, run_dir, variables,
                               f"candidate-{candidate['id']}")
        base_rows.append(left)
        candidate_rows.append(right)
        if not right["oracle"]["passed"]:
            stop = "correctness-failed"
            break
        if len(candidate_rows) >= min_pairs:
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
