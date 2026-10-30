#!/usr/bin/env python3
"""Build stable ProfileWeaver outputs from reduced Nsight Systems evidence."""

from __future__ import annotations

import json
import statistics

from nsys_sql import first, intersection_seconds, percentile, relative, seconds


def build_details(*, roi, roi_name, roi_index, range_count, devices, contexts, streams,
                  scope, wall, busy, gpu_sum, kernel_intervals, copy_intervals,
                  kernel_durations, hotspots, api, launches, shape, api_idle_ranked,
                  implications):
    """Assemble the detailed timeline artifact without further trace queries."""
    low, high = roi["start"], roi["end"]
    return {
        "protocol": "profileweaver.nsys-details/v1",
        "roi": {
            "name": roi_name, "index": roi_index, "matches": range_count,
            "start_ns": low, "end_ns": high, "wall_s": wall,
        },
        "device_ids": sorted(devices),
        "context_ids": sorted(contexts),
        "stream_ids": sorted(streams),
        "scope": scope,
        "gpu": {
            "busy_union_s": busy,
            "idle_s": max(0, wall - busy),
            "interval_sum_s": gpu_sum,
            "kernel_union_s": seconds(kernel_intervals),
            "copy_union_s": seconds(copy_intervals),
            "copy_compute_overlap_s": intersection_seconds(kernel_intervals, copy_intervals),
            "kernel_launches": len(kernel_intervals),
            "memory_operations": sum(
                item["calls"] for item in hotspots if item["kind"] == "memcpy"
            ),
            "kernel_duration_s": {
                "min": min(kernel_durations),
                "median": statistics.median(kernel_durations),
                "p95": percentile(kernel_durations, 0.95),
                "max": max(kernel_durations),
            },
        },
        "host_cuda_api": dict(api),
        "top_launches": sorted(
            launches, key=lambda item: item["duration_s"], reverse=True
        )[:50],
        "insights": {
            "timeline_shape": shape,
            "gpu_busy_pct_of_roi": busy / wall * 100 if wall else None,
            "copy_pct_of_roi": seconds(copy_intervals) / wall * 100 if wall else None,
            "dominant_pct_of_roi": hotspots[0]["pct_of_wall"] if hotspots else None,
            "dominant_amdahl_ceiling": hotspots[0]["amdahl_ceiling"] if hotspots else None,
            "api_idle_overlap_ranked": api_idle_ranked,
            "implications": implications,
            "boundary": (
                "Derived only from measured timeline intervals; API idle overlaps can "
                "overlap one another and are not summed."
            ),
        },
        "method": (
            "CUDA intervals clipped to the selected NVTX range; device intervals unioned "
            "for busy time; API time kept separate."
        ),
    }


def write_outputs(args, sqlite_path, capture, preflight, details, *, wall, busy,
                  gpu_sum, hotspots, bottleneck, scope, range_count):
    """Write detailed and normalized artifacts, then print one compact verdict."""
    args.details_output.parent.mkdir(parents=True, exist_ok=True)
    args.details_output.write_text(json.dumps(details, indent=2) + "\n")

    build = preflight.get("build") or {}
    machine = preflight.get("machine") or {}
    overhead = capture.get("overhead_factor")
    if overhead is None and capture.get("application_roi_s") and capture.get("unprofiled_median_s"):
        overhead = capture["application_roi_s"] / capture["unprofiled_median_s"]
    profile = {
        "run_id": args.run_id, "phase": args.phase, "variant": args.variant,
        "tool": {
            "name": "nsight-systems", "version": first(preflight, "version", default="unknown (not supplied)"),
            "mode": "trace", "command": first(capture, "profile_command", default="unknown (metadata not supplied)"),
            "sampling_hz": None, "events": first(capture, "events", default=["cuda", "nvtx"]),
            "overhead_factor": overhead, "multiplexed": None,
        },
        "workload": {
            "run_command": first(capture, "command", default="unknown (metadata not supplied)"),
            "build_command": build.get("command"), "compiler": build.get("compiler"),
            "build_flags": build.get("flags"),
            "input": first(capture, "input", default=preflight.get("workload")),
            "ranks": first(capture, "ranks", default=build.get("ranks")),
            "threads_per_rank": capture.get("threads_per_rank"),
            "gpus": first(capture, "gpus", default=build.get("gpus")), "nodes": capture.get("nodes"),
        },
        "machine": {
            "hostname": first(machine, "hostname", default=preflight.get("hostname")),
            "cpu_model": machine.get("cpu_model"), "cores_physical": machine.get("cores_physical"),
            "gpu_model": machine.get("gpu_model"), "memory_gb": machine.get("memory_gb"),
            "exclusive": machine.get("exclusive"), "peak_flops_gflops": None,
            "peak_bandwidth_gbs": None,
        },
        "totals": {
            "wall_time_s": wall, "cpu_time_s": None, "gpu_busy_s": busy,
            "mpi_time_s": None, "io_time_s": None, "attributed_s": gpu_sum,
            "instructions": None, "cycles": None, "ipc": None, "flops": None,
            "bytes_dram": None, "arithmetic_intensity": None, "energy_j": None,
        },
        "hotspots": hotspots, "bottleneck": bottleneck, "scope": scope,
        "warnings": [
            {"kind": "capability", "detail": "Nsight Systems timeline data does not establish occupancy, cache behavior, bandwidth ceilings, register pressure, roofline position, or instruction stalls.", "magnitude": None},
            {"kind": "attribution", "detail": "Source locations are null unless a separate symbol/source mapping stage resolves them.", "magnitude": None},
            *([{"kind": "roi-selection", "detail": f"Selected longest-first ROI index {args.roi_index} from {range_count} matching ranges.", "magnitude": range_count}] if range_count > 1 else []),
        ],
        "raw_artifacts": [relative(sqlite_path), relative(args.details_output)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2) + "\n")
    summary = {
        "status": "ok", "profile": relative(args.output),
        "details": relative(args.details_output), "roi_s": wall,
        "gpu_busy_s": busy, "bottleneck": bottleneck["class"],
        "top": hotspots[0]["name"] if hotspots else None,
        "top_pct": hotspots[0]["pct_of_wall"] if hotspots else None,
    }
    print(json.dumps(summary, separators=(",", ":")))
