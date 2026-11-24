#!/usr/bin/env python3
"""Quietly build and execute paired baseline/candidate samples."""

from __future__ import annotations

import os
import re
import subprocess
import time

from campaign_io import REPO, check_oracle, expand, extract, resolve, reporting_quantum
from campaign_stats import SEED, decision, improvement, summarize
from adaptive_policy import needs_second_pair
from screening_quality import quality


def run_command(item, label, sample_index, plan, run_dir, variables):
    item_vars = variables | {"id": item["id"], "sample": str(sample_index), "label": label,
                             "workspace": item.get("workspace", variables.get("workspace", "")),
                             "run_dir": item.get("run_dir", variables.get("run_dir", ""))}
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
    oracle_started = time.perf_counter()
    oracle = check_oracle(plan.get("oracle"), proc.returncode, stdout, stderr, cwd, env,
                          item_vars, stem)
    oracle_s = time.perf_counter() - oracle_started
    from application_phases import parse
    return {"metric": metric, "wall_s": wall_s, "oracle": oracle,
            "oracle_s": oracle_s, "application_phases": parse(stdout) if oracle["passed"] else [],
            "reporting_quantum": reporting_quantum(plan["metric"], stdout, stderr, item_vars),
            "stdout": str(stem.with_suffix(".stdout.txt")),
            "stderr": str(stem.with_suffix(".stderr.txt"))}


def run_build(item, plan, run_dir, variables):
    if not item.get("build"):
        return None
    item_vars = variables | {"id": item["id"],
                             "workspace": item.get("workspace", variables.get("workspace", "")),
                             "run_dir": item.get("run_dir", variables.get("run_dir", ""))}
    cwd = resolve(item.get("build_cwd", item.get("cwd", str(REPO))), item_vars)
    argv = [expand(x, item_vars) for x in item["build"]]
    env = os.environ.copy() | {key: expand(value, item_vars) for key, value in plan.get("env", {}).items()}
    stem = run_dir / "raw/campaign/build" / item["id"]
    stem.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                          timeout=item.get("build_timeout_s", plan.get("timeout_s")))
    stem.with_suffix(".stdout.txt").write_bytes(proc.stdout)
    stem.with_suffix(".stderr.txt").write_bytes(proc.stderr)
    if proc.returncode:
        from inspection_summary import failure
        detail = failure(stem.with_suffix(".stderr.txt"))
        missing = re.search(r"undefined reference to [`']([\w.$@]+)", detail)
        if missing:
            detail += f" | inspect --section harness --group link --candidate {item['id']} --target {missing[1]}"
        raise RuntimeError(f"build {item['id']} returned {proc.returncode}; {detail}; see {stem}.stderr.txt")
    return {"returncode": 0, "spent_s": time.perf_counter() - started,
            "stdout": str(stem.with_suffix(".stdout.txt")),
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
    if sampling.get("measurement_policy") == "single-or-two-v3":
        return bounded_candidate(base, candidate, plan, run_dir, variables)
    single_pass = (plan.get("phase") == "screen" and
                   sampling.get("mode") in {"winner-confirmation", "adaptive"})
    adaptive = single_pass and sampling.get("mode") == "adaptive"
    warmup_count = sampling.get("screen_warmup", 0) if single_pass else sampling.get("warmup", 1)
    min_pairs = sampling.get("screen_min_pairs", 1) if single_pass else sampling.get("min_pairs", 5)
    max_pairs = sampling.get("screen_max_pairs", 1) if single_pass else sampling.get("max_pairs", 9)
    warmup_rows = []
    for warmup in range(warmup_count):
        if adaptive:
            warmup_rows.append(run_command(candidate, f"candidate-{candidate['id']}-warmup-{warmup}",
                                           -warmup - 1, plan, run_dir, variables))
            if not warmup_rows[-1]["oracle"]["passed"]:
                raise RuntimeError("candidate warmup failed correctness; timed screening was not run")
        else:
            warmup_rows.extend(run_pair(base, candidate, -warmup - 1, plan, run_dir, variables,
                                        f"candidate-{candidate['id']}-warmup"))
    base_rows, candidate_rows = [], []
    stop = "maximum-pairs"
    for pair in range(max_pairs):
        left, right = run_pair(base, candidate, pair, plan, run_dir, variables,
                               f"candidate-{candidate['id']}")
        base_rows.append(left)
        candidate_rows.append(right)
        if not left["oracle"]["passed"] or not right["oracle"]["passed"]:
            stop = "correctness-failed"
            break
        if len(candidate_rows) >= min_pairs:
            if adaptive:
                observed_sampling = dict(sampling)
                if "metric_resolution" not in observed_sampling:
                    observed_sampling["metric_resolution"] = max(
                        (x.get("reporting_quantum") or 0 for x in base_rows + candidate_rows), default=0)
                if pair == 0 and needs_second_pair(
                        [x["metric"] for x in base_rows],
                        [x["metric"] for x in candidate_rows],
                        plan["metric"].get("direction", "lower"), observed_sampling):
                    continue
                stop = "screening-budget-reached" if pair else "clear-screening-result"
                break
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
            "correctness": {"passed": all(x["oracle"]["passed"] for x in base_rows + candidate_rows)},
            **({"confidence": "screening-only", "verified": False} if adaptive else {}),
            **({"ranking": quality(base_rows, candidate_rows,
                                   plan["metric"].get("direction", "lower"), sampling)} if adaptive else {}),
            "metric": summarize([x["metric"] for x in candidate_rows]),
            "baseline_metric": summarize([x["metric"] for x in base_rows]),
            "improvement": comparison, "wall": summarize([x["wall_s"] for x in candidate_rows]),
            "baseline_wall": summarize([x["wall_s"] for x in base_rows]),
            "wall_improvement": wall, "samples": {"baseline": base_rows, "candidate": candidate_rows},
            "warmup_samples": warmup_rows}


def bounded_candidate(base, candidate, plan, run_dir, variables):
    sampling = plan["sampling"]
    reference = plan.get("baseline_reference")
    limit = min(2, sampling.get("benchmark_max_runs", 2))
    baseline_reused = bool(reference)
    executions = 0
    if reference is None:
        if limit < 2:
            raise ValueError("a one-execution step requires an existing unprofiled baseline reference")
        reference = run_command(base, "baseline-reference", 0, plan, run_dir, variables)
        executions += 1
    if not reference["oracle"]["passed"]:
        raise RuntimeError("baseline reference did not pass correctness")
    rows = [run_command(candidate, "candidate-" + candidate["id"] + "-00", 0, plan, run_dir, variables)]
    executions += 1
    direction = plan["metric"].get("direction", "lower")
    observed = dict(sampling)
    observed.setdefault("metric_resolution", max(reference.get("reporting_quantum") or 0, rows[0].get("reporting_quantum") or 0))
    first_ratio = reference["metric"] / rows[0]["metric"] if direction == "lower" else rows[0]["metric"] / reference["metric"]
    unclear = needs_second_pair([reference["metric"]], [rows[0]["metric"]], direction, observed) or first_ratio < 1
    if rows[0]["oracle"]["passed"] and executions < limit and unclear:
        rows.append(run_command(candidate, "candidate-" + candidate["id"] + "-01", 1, plan, run_dir, variables))
        executions += 1
    metric = summarize([r["metric"] for r in rows])
    baseline_metric = summarize([reference["metric"]])
    point = (baseline_metric["median"] / metric["median"] if direction == "lower" else
             metric["median"] / baseline_metric["median"])
    wall = summarize([r["wall_s"] for r in rows])
    ranking = quality([reference], rows, direction, sampling)
    if ranking["status"] in {"single-pair", "provisional"}:
        ranking["status"] = "single-run" if len(rows) == 1 else "two-runs"
    ranking["baseline_reused"] = baseline_reused
    return {"id": candidate["id"], "description": candidate.get("description"), "mechanism": candidate.get("mechanism"),
            "pairs": 0, "runs": len(rows), "benchmark_executions": executions, "baseline_reused": baseline_reused,
            "stop": "execution-budget-reached" if executions == 2 else "clear-screening-result",
            "confidence": "bounded-unpaired", "verified": False,
            "correctness": {"passed": all(r["oracle"]["passed"] for r in rows)}, "ranking": ranking,
            "metric": metric, "baseline_metric": baseline_metric, "improvement": {"point": point, "ci95": None},
            "wall": wall, "baseline_wall": summarize([reference["wall_s"]]),
            "wall_improvement": {"point": reference["wall_s"] / wall["median"], "ci95": None},
            "samples": {"baseline": [reference | {"reused": baseline_reused}], "candidate": rows}, "warmup_samples": []}
