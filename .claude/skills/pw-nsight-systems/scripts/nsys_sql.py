#!/usr/bin/env python3
"""Small SQLite and interval helpers for Nsight Systems reduction."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def load(path, default=None):
    if not path:
        return default or {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default or {}


def merged(intervals):
    result = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def seconds(intervals):
    return sum(end - start for start, end in merged(intervals)) / 1e9


def intersection_seconds(left, right):
    # Sweep active intervals to avoid a quadratic Cartesian product on large traces.
    events = []
    for start, end in left:
        if end > start:
            events.extend(((start, 1, 0), (end, -1, 0)))
    for start, end in right:
        if end > start:
            events.extend(((start, 0, 1), (end, 0, -1)))
    if not events:
        return 0.0
    events.sort(key=lambda item: item[0])
    total = 0
    active_left = active_right = 0
    previous = events[0][0]
    index = 0
    while index < len(events):
        position = events[index][0]
        total += (position - previous) * active_left * active_right
        while index < len(events) and events[index][0] == position:
            _, left_delta, right_delta = events[index]
            active_left += left_delta
            active_right += right_delta
            index += 1
        previous = position
    return total / 1e9


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - index) + ordered[high] * (index - low)


def relative(path):
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path.resolve())


def first(mapping, *keys, default=None):
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def activity_scope(con, tables, roi_low, roi_high):
    """Summarize CUDA activity outside the selected ROI without changing hotspot attribution."""
    intervals = []
    copies = []
    for table in ("CUPTI_ACTIVITY_KIND_MEMCPY", "CUPTI_ACTIVITY_KIND_MEMSET",
                  "CUPTI_ACTIVITY_KIND_KERNEL"):
        if table not in tables:
            continue
        columns = "start,end" if table == "CUPTI_ACTIVITY_KIND_KERNEL" else "start,end,bytes"
        rows = con.execute(f'select {columns} from "{table}"').fetchall()
        for row in rows:
            if row[0] is None or row[1] is None or row[1] <= row[0]:
                continue
            start, end = row[0], row[1]
            intervals.append((start, end))
            if table != "CUPTI_ACTIVITY_KIND_KERNEL":
                copies.append((start, end, row[2] or 0))

    def partition(rows):
        before = [row for row in rows if row[1] <= roi_low]
        after = [row for row in rows if row[0] >= roi_high]
        overlap = [row for row in rows if row[0] < roi_high and row[1] > roi_low]
        return before, overlap, after

    before, overlap, after = partition([(start, end) for start, end in intervals])
    pre_copies, roi_copies, post_copies = partition(copies)

    def span(rows):
        return seconds((row[0], row[1]) for row in rows)

    def bytes_total(rows):
        return sum((row[2] or 0) for row in rows)

    return {
        "capture_gpu_window_s": span(intervals),
        "before_roi": {"activity_s": span(before), "events": len(before)},
        "after_roi": {"activity_s": span(after), "events": len(after)},
        "outside_roi": {"activity_s": span(before) + span(after),
                        "events": len(before) + len(after)},
        "copies": {
            "before_roi": {"union_s": span(pre_copies), "events": len(pre_copies),
                            "bytes": bytes_total(pre_copies)},
            "inside_roi": {"union_s": span(roi_copies), "events": len(roi_copies),
                           "bytes": bytes_total(roi_copies)},
            "after_roi": {"union_s": span(post_copies), "events": len(post_copies),
                           "bytes": bytes_total(post_copies)},
        },
        "policy": "outside-ROI activity is collateral evidence; ROI hotspots remain primary",
    }


def classify(requested, wall, busy, kernel_intervals, copy_intervals, hotspots, api, idle):
    idle_s, copy_s = max(0, wall - busy), seconds(copy_intervals)
    kernel_durations = [(end - start) / 1e9 for start, end in kernel_intervals]
    launch_count = len(kernel_durations)
    top = next((x for x in hotspots if x["kind"] == "gpu-kernel"), None)
    sync_idle = min(seconds(idle), sum(value["gpu_idle_overlap_s"] for name, value in api.items()
                                       if "synchron" in name.lower() or "wait" in name.lower()))
    launch_idle = min(seconds(idle), sum(value["gpu_idle_overlap_s"] for name, value in api.items()
                                         if "launch" in name.lower()))
    if requested == "auto":
        candidates = [
            (top["pct_of_wall"] / 20 if top else 0, "hotspot"),
            ((idle_s / wall) / 0.30 if wall else 0, "gpu-idle"),
            ((copy_s / wall) / 0.20 if wall else 0, "transfer"),
            ((sync_idle / wall) / 0.20 if wall else 0, "sync"),
            (min(launch_count / 50,
                 (50e-6 / statistics.median(kernel_durations)) if kernel_durations and statistics.median(kernel_durations) else 0,
                 (launch_idle / wall) / 0.05 if wall else 0), "launch-overhead"),
        ]
        requested = max(candidates)[1]
    scope = top["name"] if requested == "hotspot" and top else "selected NVTX range"
    evidence = []
    if requested == "hotspot" and top:
        evidence = [{"metric": "top_kernel_pct_of_roi", "value": top["pct_of_wall"], "threshold": "20% triage threshold",
                     "supports": "one named kernel is a substantial timeline target"}]
    elif requested == "gpu-idle":
        evidence = [{"metric": "gpu_idle_fraction", "value": idle_s / wall if wall else 0, "threshold": "30% triage threshold",
                     "supports": "the selected range contains substantial time without CUDA device activity"}]
    elif requested == "transfer":
        evidence = [{"metric": "copy_union_fraction", "value": copy_s / wall if wall else 0, "threshold": "20% triage threshold",
                     "supports": "memory operations occupy substantial non-double-counted timeline time"}]
    elif requested == "sync":
        evidence = [{"metric": "synchronization_api_gpu_idle_overlap_s", "value": sync_idle, "threshold": "20% of ROI triage threshold",
                     "supports": "synchronization or wait APIs overlap measured GPU-idle intervals"}]
    elif requested == "launch-overhead":
        evidence = [
            {"metric": "kernel_launch_count", "value": launch_count, "threshold": "50 launches triage threshold",
             "supports": "the ROI contains many kernel launches"},
            {"metric": "median_kernel_duration_s", "value": statistics.median(kernel_durations) if kernel_durations else 0,
             "threshold": "50 microseconds triage threshold", "supports": "typical kernels are short"},
            {"metric": "launch_api_gpu_idle_overlap_s", "value": launch_idle,
             "threshold": "5% of ROI triage threshold", "supports": "launch APIs overlap device-idle time"},
        ]
    else:
        requested = "indeterminate"
        evidence = [{"metric": "gpu_busy_fraction", "value": busy / wall if wall else 0, "threshold": None,
                     "supports": "timeline was measured but does not meet a configured triage rule"}]
    confidence = "high" if requested == "hotspot" and top and top["pct_of_wall"] >= 50 else "medium"
    return {"class": requested, "scope": scope, "evidence": evidence, "confidence": confidence,
            "alternatives": [{"class": "gpu-occupancy", "discriminating_measurement": "per-kernel hardware counters",
                              "suggested_child": "pw-gpa"}]}
