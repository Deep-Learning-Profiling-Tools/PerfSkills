"""Measure controller work at command boundaries and captures separately.

GPU-hours cover only a declared allocation while this command runs. An optional
exclusive cumulative joule meter is supplied by the scheduler/operator. Missing
inputs stay unknown. Nested captures inform forecasts and never enter command totals.
"""

from contextvars import ContextVar
from functools import wraps
import json
import math
import os
import resource
import time
from contextlib import contextmanager
from pathlib import Path
from statistics import median
from resource_inputs import energy, token_forecast, token_usage

from decision_learning import update
from experiment_support import load


CURRENT = ContextVar("controller_meter", default=None)


def settings(plan):
    return (plan.get("profiling", {}).get("skill_runtime") or {}).get("resources") or {}


def enabled(plan):
    from rewrite_evidence import enabled as lookahead
    return lookahead(plan)


def allocated(config):
    count = config.get("allocated_gpus")
    if count is None and config.get("exclusive_gpu_uuids"):
        count = len(config["exclusive_gpu_uuids"])
    if count is None and os.environ.get("SLURM_JOB_ID"):
        raw = os.environ.get("SLURM_GPUS_ON_NODE", "")
        count = int(raw) if raw.isdecimal() else None
    return count if type(count) is int and count >= 0 else None


def cpu():
    return sum(t.ru_utime + t.ru_stime for t in (resource.getrusage(resource.RUSAGE_SELF),
                                                 resource.getrusage(resource.RUSAGE_CHILDREN)))


@contextmanager
def measure(plan, run, scope, label):
    meta = {"label": label}
    if not enabled(plan):
        yield meta
        return
    config = settings(plan)
    start, before, joules = time.monotonic(), cpu(), energy(config)
    token = CURRENT.set((run, start, before, joules, config)) if scope == "command" else None
    try:
        yield meta
    except BaseException:
        meta["usable"] = False
        raise
    finally:
        if token is not None:
            CURRENT.reset(token)
        wall, after, count = time.monotonic() - start, energy(config), allocated(config)
        row = dict(meta, scope=scope, wall_s=wall, cpu_s=cpu()-before,
                   allocated_gpu_h=None if count is None else wall * count / 3600,
                   energy_j=after-joules if joules is not None and after is not None and after >= joules else None)
        path = run / "raw/experiment/resource-usage.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")


def stage_meter(plan, run, argv):
    from contextlib import nullcontext
    if argv and Path(argv[0]).name == "nsys" and "profile" in argv:
        return measure(plan, run, "capture", "pw-nsight-systems")
    return nullcontext({})


def capture_cost(provider):
    def decorate(fn):
        @wraps(fn)
        def wrapped(plan, run, *args, **kwargs):
            if not enabled(plan):
                return fn(plan, run, *args, **kwargs)
            with measure(plan, run, "capture", provider) as meta:
                result = fn(plan, run, *args, **kwargs)
                meta.update(label=(result.get("surface") if provider == "native" else provider) or provider,
                            usable=result.get("status") == "complete" and result.get("cache") != "hit"
                            and result.get("disposition") not in {"reused", "cache-hit"})
                return result
        return wrapped
    return decorate


def validate(plan):
    config = settings(plan)
    for name, cap in (config.get("budgets") or {}).items():
        if (name not in {"allocated_gpu_h", "cpu_s", "energy_j", "session_tokens"} or
                type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0):
            raise ValueError("resource budgets require a supported resource and positive finite cap")
    uuids = config.get("exclusive_gpu_uuids", [])
    if (not isinstance(uuids, list) or any(not isinstance(v, str) or not v.startswith("GPU-") for v in uuids)
            or len(set(uuids)) != len(uuids)):
        raise ValueError("exclusive_gpu_uuids requires unique physical GPU UUIDs")
    if config.get("exclusive_energy_j_path") and not Path(config["exclusive_energy_j_path"]).is_absolute():
        raise ValueError("exclusive energy meter path must be absolute")
    floor = (plan.get("profiling", {}).get("skill_runtime") or {}).get("minimum_expected_log_gain")
    if floor is not None and (type(floor) not in (int, float) or not math.isfinite(floor) or floor <= 0):
        raise ValueError("minimum_expected_log_gain must be positive and finite")
    if "allocated_gpus" in config and allocated(config) is None:
        raise ValueError("allocated_gpus must be a nonnegative integer")


def ledger(run):
    path = run / "raw/experiment/resource-usage.jsonl"
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def totals(run, rows=None):
    commands = [r for r in (ledger(run) if rows is None else rows) if r["scope"] == "command"]
    running = CURRENT.get()
    if running and running[0] == run:
        _, start, before, joules, config = running
        wall, count, after = time.monotonic()-start, allocated(config), energy(config)
        commands.append({"wall_s": wall, "cpu_s": cpu()-before,
                         "allocated_gpu_h": wall * count / 3600 if count is not None else None,
                         "energy_j": after-joules if joules is not None and after is not None and after >= joules else None})
    result = {}
    for key in ("wall_s", "cpu_s", "allocated_gpu_h", "energy_j"):
        values = [r.get(key) for r in commands]
        result[key] = sum(values) if values and None not in values else None
    return dict(result, commands=len(commands), session_tokens=load(run / "session-usage.json", {}).get("session_tokens"))


def price(plan, rows, spent, provider, wall, packet_tokens):
    """Additional cost as a fraction of the remaining user-declared budgets.

    Client session intervals forecast token cost when available.
    Hard resource caps with unknown consumption cannot authorize optional capture.
    """
    samples = [r for r in rows if r["scope"] == "capture" and r["label"] == provider and r.get("usable", True) and r.get("wall_s", 0) > 0]
    count = allocated(settings(plan))
    estimate = {"allocated_gpu_h": None if count is None else wall * count / 3600,
                "session_tokens": token_forecast(Path(plan["run_dir"]), provider, packet_tokens)}
    for key in ("cpu_s", "energy_j"):
        rates = [r[key] / r["wall_s"] for r in samples if r.get(key) is not None]
        estimate[key] = median(rates) * wall if rates else None
    penalty = 0.
    for key, limit in (settings(plan).get("budgets") or {}).items():
        used, need = spent.get(key), estimate.get(key)
        if used is None or need is None or need > limit - used or limit <= used:
            return estimate, None
        penalty += need / (limit - used)
    return estimate, penalty
