#!/usr/bin/env python3
"""Reduce an Nsight Systems SQLite export to ProfileWeaver evidence.

The reducer clips CUDA activity to one NVTX range, unions overlapping device intervals, and
keeps raw evidence on disk. It emits one normalized profile, one detailed metrics artifact,
and only a compact status line to stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
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
    return seconds((max(a, c), min(b, d)) for a, b in left for c, d in right if max(a, c) < min(b, d))


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
            group["intervals"].append((start, end)); group["calls"] += 1
            if "bytes" in keys and row["bytes"] is not None:
                group["bytes"] += row["bytes"]
            all_intervals.append((start, end))
    if not kernel_intervals:
        raise SystemExit("selected NVTX range has no CUDA kernel activity")

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
        kept.append(item); cumulative += item["self_time_s"]
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
            api[name]["calls"] += 1; api[name]["thread_time_s"] += (end - start) / 1e9
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
    details = {
        "protocol": "profileweaver.nsys-details/v1", "roi": {"name": args.roi_name, "index": args.roi_index,
        "matches": len(ranges), "start_ns": low, "end_ns": high, "wall_s": wall},
        "device_ids": sorted(devices), "context_ids": sorted(contexts), "stream_ids": sorted(streams),
        "gpu": {"busy_union_s": busy, "idle_s": max(0, wall - busy), "interval_sum_s": gpu_sum,
                "kernel_union_s": seconds(kernel_intervals), "copy_union_s": seconds(copy_intervals),
                "copy_compute_overlap_s": intersection_seconds(kernel_intervals, copy_intervals),
                "kernel_launches": len(kernel_intervals), "memory_operations": sum(x["calls"] for x in hotspots if x["kind"] == "memcpy"),
                "kernel_duration_s": {"min": min(kernel_durations), "median": statistics.median(kernel_durations),
                                      "p95": percentile(kernel_durations, .95), "max": max(kernel_durations)}},
        "host_cuda_api": dict(api), "top_launches": sorted(launches, key=lambda x: x["duration_s"], reverse=True)[:50],
        "insights": {
            "timeline_shape": shape,
            "gpu_busy_pct_of_roi": busy / wall * 100 if wall else None,
            "copy_pct_of_roi": seconds(copy_intervals) / wall * 100 if wall else None,
            "dominant_pct_of_roi": dominant["pct_of_wall"] if dominant else None,
            "dominant_amdahl_ceiling": dominant["amdahl_ceiling"] if dominant else None,
            "api_idle_overlap_ranked": api_idle_ranked,
            "implications": implications,
            "boundary": "Derived only from measured timeline intervals; API idle overlaps can overlap one another and are not summed.",
        },
        "method": "CUDA intervals clipped to the selected NVTX range; device intervals unioned for busy time; API time kept separate.",
    }
    args.details_output.parent.mkdir(parents=True, exist_ok=True)
    args.details_output.write_text(json.dumps(details, indent=2) + "\n")

    tool_version = first(preflight, "version", default="unknown (not supplied)")
    build = preflight.get("build") or {}
    machine = preflight.get("machine") or {}
    overhead = capture.get("overhead_factor")
    if overhead is None and capture.get("application_roi_s") and capture.get("unprofiled_median_s"):
        overhead = capture["application_roi_s"] / capture["unprofiled_median_s"]
    profile = {
        "run_id": args.run_id, "phase": args.phase, "variant": args.variant,
        "tool": {"name": "nsight-systems", "version": tool_version, "mode": "trace",
                 "command": first(capture, "profile_command", default="unknown (metadata not supplied)"),
                 "sampling_hz": None, "events": first(capture, "events", default=["cuda", "nvtx"]),
                 "overhead_factor": overhead, "multiplexed": None},
        "workload": {"run_command": first(capture, "command", default="unknown (metadata not supplied)"),
                     "build_command": build.get("command"), "compiler": build.get("compiler"), "build_flags": build.get("flags"),
                     "input": first(capture, "input", default=preflight.get("workload")), "ranks": first(capture, "ranks", default=build.get("ranks")),
                     "threads_per_rank": capture.get("threads_per_rank"), "gpus": first(capture, "gpus", default=build.get("gpus")),
                     "nodes": capture.get("nodes")},
        "machine": {"hostname": first(machine, "hostname", default=preflight.get("hostname")), "cpu_model": machine.get("cpu_model"),
                    "cores_physical": machine.get("cores_physical"), "gpu_model": machine.get("gpu_model"), "memory_gb": machine.get("memory_gb"),
                    "exclusive": machine.get("exclusive"), "peak_flops_gflops": None, "peak_bandwidth_gbs": None},
        "totals": {"wall_time_s": wall, "cpu_time_s": None, "gpu_busy_s": busy, "mpi_time_s": None, "io_time_s": None,
                   "attributed_s": gpu_sum, "instructions": None, "cycles": None, "ipc": None, "flops": None,
                   "bytes_dram": None, "arithmetic_intensity": None, "energy_j": None},
        "hotspots": kept, "bottleneck": bottleneck,
        "warnings": [
            {"kind": "capability", "detail": "Nsight Systems timeline data does not establish occupancy, cache behavior, bandwidth ceilings, register pressure, roofline position, or instruction stalls.", "magnitude": None},
            {"kind": "attribution", "detail": "Source locations are null unless a separate symbol/source mapping stage resolves them.", "magnitude": None},
            *([{"kind": "roi-selection", "detail": f"Selected longest-first ROI index {args.roi_index} from {len(ranges)} matching ranges.", "magnitude": len(ranges)}] if len(ranges) > 1 else []),
        ],
        "raw_artifacts": [relative(sqlite_path), relative(args.details_output)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2) + "\n")
    print(json.dumps({"status": "ok", "profile": relative(args.output), "details": relative(args.details_output),
                      "roi_s": wall, "gpu_busy_s": busy, "bottleneck": bottleneck["class"],
                      "top": kept[0]["name"] if kept else None, "top_pct": kept[0]["pct_of_wall"] if kept else None},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
