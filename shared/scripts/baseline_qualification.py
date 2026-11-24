"""Check a cached reference against existing diagnostic output, never rank by it."""

import math
import re

from experiment_support import load, write


def reported_facts(stdout):
    """Preserve bounded categorical output verbatim; do not infer active branches."""
    rows = []
    for line in stdout.splitlines():
        match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9 ()/_-]{0,63}):[ \t]+([A-Za-z][^\r\n]{0,95}?)\s*", line)
        if match:
            row = {"label": match[1].strip(), "value": match[2].strip()}
            if row not in rows:
                rows.append(row)
        if len(rows) == 8:
            break
    return rows


def qualify(plan, run, repeat):
    """One conditional baseline refresh, with a hard total of two unprofiled runs.

    An instrumented application reporting a substantially *better* value than the
    unprofiled reference is suspicious. Ordinary profiler slowdown is expected.
    Compare only the same application metric, never NVTX/CUPTI region durations.
    """
    if (plan.get("sampling") or {}).get("measurement_policy") != "single-or-two-v3":
        return
    path = run / "baseline-timing.json"
    doc = load(path, {})
    if not doc.get("sample") or doc.get("qualification"):
        return
    selection = plan.get("selection_objective") or {}
    if selection.get("scope") == "kernel" and selection.get("collateral") == "profiled":
        doc["qualification"] = {"status":"kernel-primary-profiled-collateral","unprofiled_runs":1}
        write(path,doc)
        return
    first = doc["sample"]
    samples = doc.get("samples") or [first]
    diagnostic = load(run / "baseline-acquisition.json", {}).get("diagnostic_metric")
    metric = plan.get("metric") or {}
    quality = {"status": "not-compared", "unprofiled_runs": len(samples),
               "diagnostic_metric": diagnostic, "diagnostic_role": "instrumented-not-for-ranking"}
    if metric.get("source") not in {"stdout", "stderr"} or not all(
            isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            for v in (first.get("metric"), diagnostic)):
        doc["qualification"] = quality
        write(path, doc)
        return
    lower = metric.get("direction", "lower") == "lower"

    def ratio(value):
        return value / diagnostic if lower else diagnostic / value

    quality["trigger_ratio"] = ratio(first["metric"])
    if quality["trigger_ratio"] > 1.25 and len(samples) < 2:
        samples.append(repeat())
    # Do not select the instrumented observation as a baseline. The conservative
    # unprofiled reference cannot inflate the claimed improvement over either run.
    selected = (min if lower else max)(samples, key=lambda x: x["metric"])
    quality.update({"status": "unresolved" if ratio(selected["metric"]) > 1.25 else
                   "refreshed" if len(samples) > 1 else "consistent",
                   "unprofiled_runs": len(samples),
                   "reference_to_diagnostic_ratio": ratio(selected["metric"]),
                   "selection": "conservative-unprofiled-reference"})
    doc.update({"sample": selected, "samples": samples, "qualification": quality})
    write(path, doc)
