"""Execution and robust statistics for bench.py."""

import random
import statistics
import subprocess
import time

MIN_REPS = 5
BOOTSTRAP_ITERS = 10000
BOOTSTRAP_SEED = 20260825

def run_once(cmd, cwd, env):
    """Run cmd, return (wall_seconds, returncode, tail_of_stderr)."""
    start = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.perf_counter() - start
    err = proc.stderr.decode("utf-8", "replace")[-2000:]
    return elapsed, proc.returncode, err

def check_oracle(oracle, cwd, env):
    if not oracle:
        return None
    proc = subprocess.run(oracle, cwd=cwd, env=env, shell=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {
        "returncode": proc.returncode,
        "output": proc.stdout.decode("utf-8", "replace")[-2000:],
    }

def bootstrap_median_ci(samples, rng, iters=BOOTSTRAP_ITERS):
    if len(samples) < 2:
        return None
    n = len(samples)
    medians = sorted(statistics.median(rng.choices(samples, k=n)) for _ in range(iters))
    return [medians[int(0.025 * iters)], medians[int(0.975 * iters)]]

def bootstrap_speedup_ci(baseline, variant, rng, iters=BOOTSTRAP_ITERS):
    """CI on baseline_median / variant_median by independent resampling."""
    if len(baseline) < 2 or len(variant) < 2:
        return None
    nb, nv = len(baseline), len(variant)
    ratios = []
    for _ in range(iters):
        b = statistics.median(rng.choices(baseline, k=nb))
        v = statistics.median(rng.choices(variant, k=nv))
        if v > 0:
            ratios.append(b / v)
    if not ratios:
        return None
    ratios.sort()
    return [ratios[int(0.025 * len(ratios))], ratios[int(0.975 * len(ratios))]]

def summarize(samples, rng):
    med = statistics.median(samples)
    mad = statistics.median([abs(s - med) for s in samples]) if len(samples) > 1 else 0.0
    return {
        "median_s": med,
        "mad_s": mad,
        "rel_mad": (mad / med) if med > 0 else None,
        "min_s": min(samples),
        "max_s": max(samples),
        "mean_s": statistics.fmean(samples),
        "stdev_s": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "ci95_s": bootstrap_median_ci(samples, rng),
    }

def campaign(label, cmd, reps, warmup, cwd, env, oracle, peer_cmd=None, peer_label=None,
             adaptive=False, min_reps=MIN_REPS, min_speedup=1.02):
    """Run paired samples, optionally stopping once the speedup interval is decisive."""
    samples, peer_samples, failures = [], [], []
    correctness = None
    stopping = {"mode": "adaptive" if adaptive else "fixed", "reason": "fixed-repetitions",
                "min_reps": min_reps if adaptive else None, "max_reps": reps,
                "practical_speedup_threshold": min_speedup if adaptive else None,
                "interim_ci95": None}

    for i in range(warmup):
        run_once(cmd, cwd, env)
        if peer_cmd:
            run_once(peer_cmd, cwd, env)

    for i in range(reps):
        if peer_cmd:  # A first, then B, so drift affects both equally
            t, rc, err = run_once(peer_cmd, cwd, env)
            if rc != 0:
                failures.append({"rep": i, "label": peer_label, "returncode": rc, "stderr": err})
            else:
                peer_samples.append(t)

        t, rc, err = run_once(cmd, cwd, env)
        if rc != 0:
            failures.append({"rep": i, "label": label, "returncode": rc, "stderr": err})
            continue
        samples.append(t)

        if oracle:
            res = check_oracle(oracle, cwd, env)
            if res["returncode"] != 0:
                correctness = {"status": "failed", "oracle": oracle,
                               "detail": f"rep {i}: {res['output']}"}
                failures.append({"rep": i, "label": label, "oracle_failed": True})
            elif correctness is None or correctness.get("status") == "passed":
                correctness = {"status": "passed", "oracle": oracle,
                               "detail": f"passed on all reps through {i}"}

        if adaptive and peer_cmd and len(samples) >= min_reps and len(peer_samples) >= min_reps:
            interim_rng = random.Random(BOOTSTRAP_SEED + len(samples))
            ci = bootstrap_speedup_ci(peer_samples, samples, interim_rng, iters=2000)
            stopping["interim_ci95"] = ci
            if correctness and correctness.get("status") == "failed":
                stopping["reason"] = "correctness-failed"
                break
            if ci and ci[0] >= min_speedup:
                stopping["reason"] = "decisive-improvement"
                break
            if ci and ci[1] <= 1 / min_speedup:
                stopping["reason"] = "decisive-regression"
                break
            if ci and ci[0] >= 1 / min_speedup and ci[1] <= min_speedup:
                stopping["reason"] = "practical-equivalence"
                break
    if adaptive and stopping["reason"] == "fixed-repetitions":
        stopping["reason"] = "maximum-repetitions"
    stopping["reps_used"] = len(samples)

    return samples, peer_samples, failures, correctness, stopping
