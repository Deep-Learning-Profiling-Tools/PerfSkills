#!/usr/bin/env python3
"""Small robust summaries and paired bootstrap decisions."""

from __future__ import annotations

import random
import statistics as st

SEED = 20261011


def quantile(values, fraction):
    return values[min(len(values) - 1, max(0, int(fraction * len(values))))]


def summarize(values):
    median = st.median(values)
    return {"samples": values, "median": median,
            "mad": st.median(abs(value - median) for value in values),
            "min": min(values), "max": max(values)}


def improvement(base, candidate, direction, iterations, seed):
    """Paired bootstrap CI for the ratio of medians in the improvement direction."""
    if len(base) != len(candidate) or not base:
        return None
    if len(base) == 1:
        b, c = base[0], candidate[0]
        point = (b / c) if direction == "lower" else (c / b)
        return {"point": point, "ci95": [point, point],
                "confidence": "single-sample-screen"}
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
