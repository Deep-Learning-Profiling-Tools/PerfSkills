#!/usr/bin/env python3
"""Timing harness for ProfileWeaver child skills.

Runs a command N times, reports robust statistics, and emits a record conforming to
shared/schema/benchmark.schema.json. Optionally interleaves against a baseline command
(A/B/A/B) so machine drift shows up as noise rather than as a result, and optionally
runs a correctness oracle after every repetition.

Stdlib only. Examples:

    # baseline campaign
    bench.py --label baseline --reps 5 --out runs/R/bench.baseline.json -- ./app input.deck

    # variant vs. an existing baseline record
    bench.py --label variant-tiling --reps 7 --baseline runs/R/bench.baseline.json \\
             --oracle "python3 check.py out.dat ref.dat" -- ./app.tiled input.deck

    # interleaved A/B in one campaign (preferred: immune to drift)
    bench.py --label variant-tiling --reps 7 --baseline-command "./app input.deck" \\
             --out runs/R/bench.tiling.json -- ./app.tiled input.deck
"""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

MIN_REPS = 5
BOOTSTRAP_ITERS = 10000
BOOTSTRAP_SEED = 20260825  # fixed so a record is reproducible from its samples


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


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True, help="baseline, or variant-<name>")
    p.add_argument("--reps", type=int, default=MIN_REPS)
    p.add_argument("--adaptive", action="store_true",
                   help="stop paired A/B once the CI proves improvement, regression, or practical equivalence")
    p.add_argument("--min-reps", type=int, default=MIN_REPS)
    p.add_argument("--max-reps", type=int, default=None,
                   help="adaptive cap (default: --reps)")
    p.add_argument("--min-speedup", type=float, default=1.02,
                   help="smallest practically meaningful speedup for adaptive stopping")
    p.add_argument("--warmup", type=int, default=1, help="reps run and discarded first")
    p.add_argument("--cwd", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--oracle", default=None, help="shell cmd; non-zero exit = correctness failure")
    p.add_argument("--tolerance", default=None, help="documented tolerance of the oracle")
    p.add_argument("--baseline", default=None, help="path to an existing baseline record, for speedup")
    p.add_argument("--baseline-command", default=None,
                   help="baseline cmd to interleave A/B against (preferred over --baseline)")
    p.add_argument("--baseline-label", default="baseline")
    p.add_argument("--out", default=None, help="output JSON path (default: stdout only)")
    p.add_argument("--note", default=None)
    p.add_argument("--compact-stdout", action="store_true",
                   help="write the full record to --out and print only a one-line status")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command to time")
    a = p.parse_args()

    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        p.error("no command given; put it after --")
    if a.compact_stdout and not a.out:
        p.error("--compact-stdout requires --out so the full evidence is preserved")
    reps = a.max_reps if a.adaptive and a.max_reps is not None else a.reps
    if a.adaptive and not a.baseline_command:
        p.error("--adaptive requires --baseline-command for paired decisions")
    if a.adaptive and (a.min_reps < MIN_REPS or a.min_reps > reps):
        p.error(f"--min-reps must be between {MIN_REPS} and the repetition cap")
    if a.min_speedup <= 1:
        p.error("--min-speedup must exceed 1.0")
    if reps < MIN_REPS:
        print(f"warning: reps={reps} is below the schema minimum of {MIN_REPS}; "
              f"this record cannot support a significance claim.", file=sys.stderr)

    rng = random.Random(BOOTSTRAP_SEED)
    env = os.environ.copy()
    run_id = a.run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-bench")

    samples, peer_samples, failures, correctness, stopping = campaign(
        a.label, cmd, reps, a.warmup, a.cwd, env, a.oracle,
        peer_cmd=a.baseline_command, peer_label=a.baseline_label,
        adaptive=a.adaptive, min_reps=a.min_reps, min_speedup=a.min_speedup)

    if not samples:
        print("error: no successful runs; nothing to report", file=sys.stderr)
        print(json.dumps({"failures": failures}, indent=2))
        return 1

    if correctness is None:
        correctness = {"status": "unverified", "oracle": None,
                       "detail": "no oracle supplied; timing alone cannot support a rewrite"}
    correctness["tolerance"] = a.tolerance

    rec = {
        "run_id": run_id,
        "label": a.label,
        "command": " ".join(cmd),
        "cwd": a.cwd or os.getcwd(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "reps": len(samples),
        "warmup_discarded": a.warmup,
        "interleaved_with": a.baseline_label if a.baseline_command else None,
        "samples_s": samples,
        "correctness": correctness,
        "conditions": {
            "hostname": os.uname().nodename,
            "exclusive": None,
            "notes": a.note,
        },
        "failures": failures,
        "stopping": stopping,
    }
    rec.update(summarize(samples, rng))

    # Speedup: prefer the interleaved peer, fall back to a stored baseline record.
    base_samples, base_label, base_median = None, None, None
    if peer_samples:
        base_samples, base_label = peer_samples, a.baseline_label
    elif a.baseline:
        with open(a.baseline) as f:
            prior = json.load(f)
        base_samples = prior.get("samples_s")
        base_label = prior.get("label", "baseline")
        base_median = prior.get("median_s")

    if base_samples:
        base_median = statistics.median(base_samples)
        ci = bootstrap_speedup_ci(base_samples, samples, rng)
        point = base_median / rec["median_s"] if rec["median_s"] > 0 else None
        rec["speedup"] = {
            "baseline_label": base_label,
            "baseline_median_s": base_median,
            "point": point,
            "ci95": ci,
            # significant only if the whole interval sits on one side of 1.0
            "significant": bool(ci and (ci[0] > 1.0 or ci[1] < 1.0)),
            "practically_significant": bool(ci and
                                            (ci[0] >= a.min_speedup or ci[1] <= 1 / a.min_speedup)),
            "practical_threshold": a.min_speedup,
        }

    out = json.dumps(rec, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            f.write(out + "\n")

        if peer_samples:  # interleaving produced a baseline record too; keep it
            peer_rec = {
                "run_id": run_id, "label": base_label,
                "command": a.baseline_command, "cwd": rec["cwd"],
                "timestamp_utc": rec["timestamp_utc"], "reps": len(peer_samples),
                "warmup_discarded": a.warmup, "interleaved_with": a.label,
                "samples_s": peer_samples, "conditions": rec["conditions"],
                "correctness": None, "failures": [],
            }
            peer_rec.update(summarize(peer_samples, rng))
            peer_path = a.out.replace(".json", "") + ".baseline.json"
            with open(peer_path, "w") as f:
                f.write(json.dumps(peer_rec, indent=2) + "\n")

    if a.compact_stdout:
        sp = rec.get("speedup") or {}
        print(json.dumps({"status": "ok", "label": a.label, "reps": rec["reps"],
                          "speedup": sp.get("point"), "ci95": sp.get("ci95"),
                          "correctness": correctness["status"], "stop": stopping["reason"],
                          "output": a.out}, separators=(",", ":")))
    else:
        print(out)

    sp = rec.get("speedup")
    if sp:
        verdict = "SIGNIFICANT" if sp["significant"] else "NOT SIGNIFICANT (CI includes 1.0)"
        print(f"\n{a.label}: {sp['point']:.3f}x  CI95 "
              f"[{sp['ci95'][0]:.3f}, {sp['ci95'][1]:.3f}]  {verdict}", file=sys.stderr)
    if rec["rel_mad"] and rec["rel_mad"] > 0.05:
        print(f"warning: rel_mad={rec['rel_mad']:.3f} - noisy machine. Treat deltas below "
              f"this as zero, or fix the measurement first.", file=sys.stderr)
    if correctness["status"] != "passed":
        print(f"warning: correctness {correctness['status']} - not a reportable rewrite.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
