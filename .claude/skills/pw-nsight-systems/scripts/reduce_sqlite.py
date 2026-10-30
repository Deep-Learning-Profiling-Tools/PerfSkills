#!/usr/bin/env python3
"""Reduce a bounded Nsight Systems capture into normalized evidence."""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

from nsys_output import build_details, write_outputs
from nsys_sql import (
    activity_scope,
    classify,
    first,
    intersection_seconds,
    load,
    merged,
    seconds,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=["baseline", "variant", "reprofile"], default="baseline")
    parser.add_argument("--variant")
    parser.add_argument("--roi-name", default="hpcperf:roi")
    parser.add_argument("--roi-index", type=int, default=0, help="index after sorting matching ranges longest-first")
    parser.add_argument("--question-class", choices=["auto", "hotspot", "gpu-idle", "transfer", "launch-overhead", "sync"], default="auto")
    parser.add_argument("--capture-metadata", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--details-output", type=Path, required=True)
    parser.add_argument("--max-hotspots", type=int, default=30)
    args = parser.parse_args()

    sqlite_path = args.sqlite.resolve()
    capture, preflight = load(args.capture_metadata), load(args.preflight)
    con = sqlite3.connect(sqlite_path)
    con.row_factory = sqlite3.Row
    tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    if "StringIds" not in tables or "NVTX_EVENTS" not in tables:
        raise SystemExit("SQLite export lacks StringIds or NVTX_EVENTS; verify nsys export")
    strings = {row["id"]: row["value"] for row in con.execute("select * from StringIds")}

    def event_name(row):
        for key in ("text", "demangledName", "shortName", "nameId", "textId"):
            if key in row.keys() and row[key] is not None:
                return strings.get(row[key], str(row[key]))
        return "unresolved"

    ranges = [row for row in con.execute("select * from NVTX_EVENTS")
              if event_name(row) == args.roi_name and row["end"] is not None and row["end"] > row["start"]]
    ranges.sort(key=lambda row: row["end"] - row["start"], reverse=True)
    if not ranges or args.roi_index >= len(ranges):
        raise SystemExit(f"no complete NVTX range named {args.roi_name!r} at index {args.roi_index}")
    roi = ranges[args.roi_index]
    low, high = roi["start"], roi["end"]
    wall = (high - low) / 1e9
    copy_labels = {}
    if "ENUM_CUDA_MEMCPY_OPER" in tables:
        copy_labels = {row["id"]: row["label"] for row in con.execute("select * from ENUM_CUDA_MEMCPY_OPER")}

    groups = defaultdict(lambda: {"intervals": [], "calls": 0, "bytes": 0})
    kernel_intervals, copy_intervals, all_intervals, launches = [], [], [], []
    devices, contexts, streams = set(), set(), set()
    for table, kind in (("CUPTI_ACTIVITY_KIND_KERNEL", "gpu-kernel"),
                        ("CUPTI_ACTIVITY_KIND_MEMCPY", "memcpy"),
                        ("CUPTI_ACTIVITY_KIND_MEMSET", "memcpy")):
        if table not in tables:
            continue
        for row in con.execute(f'select * from "{table}" where start < ? and end > ?', (high, low)):
            start, end = max(low, row["start"]), min(high, row["end"])
            keys = row.keys()
            if "deviceId" in keys:
                devices.add(row["deviceId"])
            if "contextId" in keys:
                contexts.add(row["contextId"])
            if "streamId" in keys:
                streams.add(row["streamId"])
            if kind == "gpu-kernel":
                name = event_name(row)
                kernel_intervals.append((start, end))
                launches.append({"name": name, "duration_s": (end - start) / 1e9,
                                 "grid": [row[x] for x in ("gridX", "gridY", "gridZ") if x in keys],
                                 "block": [row[x] for x in ("blockX", "blockY", "blockZ") if x in keys]})
            else:
                name = "CUDA memset" if table.endswith("MEMSET") else "CUDA memcpy " + copy_labels.get(row["copyKind"], f"kind={row['copyKind']}")
                copy_intervals.append((start, end))
            group = groups[(kind, name)]
            group["intervals"].append((start, end))
            group["calls"] += 1
            if "bytes" in keys and row["bytes"] is not None:
                group["bytes"] += row["bytes"]
            all_intervals.append((start, end))
    if not kernel_intervals:
        raise SystemExit("selected NVTX range has no CUDA kernel activity")

    scope = activity_scope(con, tables, low, high)

    busy = seconds(all_intervals)
    gpu_sum = sum(end - start for start, end in all_intervals) / 1e9
    hotspots = []
    for (kind, name), values in groups.items():
        own = sum(end - start for start, end in values["intervals"]) / 1e9
        hotspots.append({"name": name, "kind": kind, "file": None, "line": None,
                         "module": first(capture, "executable", default=(capture.get("command") or "").split(" ", 1)[0] or None),
                         "self_time_s": own, "total_time_s": None, "pct_of_wall": own / wall * 100,
                         "calls": values["calls"], "samples": None, "callpath": None,
                         "metrics": {"bytes": values["bytes"] if kind == "memcpy" else None},
                         "amdahl_ceiling": None if own >= wall else 1 / (1 - own / wall)})
    hotspots.sort(key=lambda item: item["self_time_s"], reverse=True)
    kept, cumulative = [], 0.0
    target_coverage = 0.80 * gpu_sum
    for item in hotspots:
        item["rank"] = len(kept) + 1
        kept.append(item)
        cumulative += item["self_time_s"]
        if (cumulative >= target_coverage and len(kept) >= 1) or len(kept) >= args.max_hotspots:
            break

    busy_union = merged(all_intervals)
    idle, cursor = [], low
    for start, end in busy_union:
        if cursor < start:
            idle.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < high:
        idle.append((cursor, high))
    api, api_intervals = defaultdict(lambda: {"calls": 0, "thread_time_s": 0.0}), defaultdict(list)
    for table in ("CUPTI_ACTIVITY_KIND_RUNTIME", "CUPTI_ACTIVITY_KIND_DRIVER"):
        if table not in tables:
            continue
        for row in con.execute(f'select * from "{table}" where start < ? and end > ?', (high, low)):
            name, start, end = event_name(row), max(low, row["start"]), min(high, row["end"])
            api[name]["calls"] += 1
            api[name]["thread_time_s"] += (end - start) / 1e9
            api_intervals[name].append((start, end))
    for name, intervals in api_intervals.items():
        api[name]["gpu_idle_overlap_s"] = intersection_seconds(intervals, idle)

    kernel_durations = [(end - start) / 1e9 for start, end in kernel_intervals]
    bottleneck = classify(args.question_class, wall, busy, kernel_intervals, copy_intervals, hotspots, api, idle)
    dominant = hotspots[0] if hotspots else None
    shape = []
    if dominant and dominant["pct_of_wall"] >= 60:
        shape.append("dominant-long-kernel")
    if wall and busy / wall < 0.5:
        shape.append("gpu-gap-heavy")
    if wall and seconds(copy_intervals) / wall >= 0.2:
        shape.append("copy-heavy")
    if len(kernel_intervals) >= 100 and statistics.median(kernel_durations) < 50e-6:
        shape.append("fragmented-small-kernels")
    if not shape:
        shape.append("mixed-timeline")
    implications = []
    if "dominant-long-kernel" in shape:
        implications.append({
            "measured_fact": f"{dominant['name']} accounts for {dominant['pct_of_wall']:.2f}% of ROI",
            "supports": "prioritize changes to this kernel or its preparation pipeline",
            "does_not_establish": "the kernel-internal compute, memory, cache, occupancy, or stall cause",
        })
    if wall and busy / wall >= 0.8 and seconds(copy_intervals) / wall < 0.05:
        implications.append({
            "measured_fact": f"GPU activity covers {busy / wall * 100:.2f}% of ROI and copies cover {seconds(copy_intervals) / wall * 100:.2f}%",
            "supports": "GPU gaps and transfers are not the first timeline-level target",
            "does_not_establish": "kernel-internal efficiency",
        })
    if "fragmented-small-kernels" in shape:
        implications.append({
            "measured_fact": f"{len(kernel_intervals)} launches with median duration {statistics.median(kernel_durations) * 1e6:.2f} us",
            "supports": "test launch-count reduction or fusion while retaining end-to-end timing",
            "does_not_establish": "that launch overhead exceeds useful device work",
        })
    api_idle_ranked = sorted(
        ({"name": name, "calls": values["calls"],
          "idle_overlap_s": values.get("gpu_idle_overlap_s", 0.0),
          "thread_time_s": values["thread_time_s"]} for name, values in api.items()),
        key=lambda item: item["idle_overlap_s"], reverse=True,
    )[:10]
    details = build_details(
        roi=roi, roi_name=args.roi_name, roi_index=args.roi_index,
        range_count=len(ranges), devices=devices, contexts=contexts, streams=streams,
        scope=scope, wall=wall, busy=busy, gpu_sum=gpu_sum,
        kernel_intervals=kernel_intervals, copy_intervals=copy_intervals,
        kernel_durations=kernel_durations, hotspots=hotspots, api=api,
        launches=launches, shape=shape, api_idle_ranked=api_idle_ranked,
        implications=implications,
    )
    write_outputs(
        args, sqlite_path, capture, preflight, details, wall=wall, busy=busy,
        gpu_sum=gpu_sum, hotspots=kept, bottleneck=bottleneck, scope=scope,
        range_count=len(ranges),
    )

if __name__ == "__main__":
    raise SystemExit(main())
