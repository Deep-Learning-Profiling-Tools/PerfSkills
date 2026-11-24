"""Label the workload timer alongside CUPTI device activity in one observation."""

import math

from experiment_support import REPO, load, write


def enabled(plan):
    metric = plan.get("metric") or {}
    timing = plan.get("kernel_timing") or {}
    return timing.get("source") == "cuda-event" and metric.get("source") in {"stdout", "stderr", "file"}


def _time(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _baseline_time(run):
    sample = load(run / "baseline-timing.json", {}).get("sample") or {}
    if (sample.get("oracle") or {}).get("passed") and not sample.get("profiled"):
        return _time(sample.get("metric"))


def _timer(plan):
    metric = plan["metric"]
    region = plan["kernel_timing"].get("detail") or metric.get("name")
    return {"source": "cuda-event", "unit": metric.get("unit"), "region": region[:100] if region else None}


def baseline(plan, run, packet):
    """Join the two timing scopes in the regular start response."""
    if not enabled(plan) or not packet:
        return packet
    capture = load(run / "baseline-acquisition.json", {})
    elapsed = _time(capture.get("diagnostic_metric"))
    if capture.get("provider") == "pw-cupti" and elapsed is not None:
        view = packet["baseline_observation"]["observations"]
        view["cuda_event"] = _timer(plan) | {"unprofiled": _baseline_time(run), "during_cupti": elapsed}
    return packet


def retain(plan, capsule):
    """Store the application timer with the measured profile before it enters the cache."""
    if not enabled(plan) or capsule.get("surface") != "pw-cupti" or capsule.get("status") != "complete":
        return
    elapsed = _time(capsule.get("diagnostic_metric"))
    if elapsed is not None:
        path = REPO / capsule["profile_artifact"]
        profile = load(path, {})
        profile["cuda_event_elapsed"] = elapsed
        write(path, profile)


def build(plan, run, state, capsule, row, **options):
    """Add the CUDA-event diagnostic through the existing bounded packet renderer."""
    from iteration_evidence import build as render

    if enabled(plan) and capsule.get("surface") == "pw-cupti" and capsule.get("status") == "complete":
        elapsed = _time(capsule.get("diagnostic_metric"))
        if elapsed is None and capsule.get("profile_artifact"):
            elapsed = _time(load(REPO / capsule["profile_artifact"], {}).get("cuda_event_elapsed"))
        if elapsed is not None:
            sample = _timer(plan) | {"during_cupti": elapsed}
            row = row | {"ranking": (row.get("ranking") or {}) | {"profiled_metric": sample}}
            if capsule["id"] == "baseline":
                row["metric"] = {"median": _baseline_time(run)}
    return render(plan, run, state, capsule, row, **options)
