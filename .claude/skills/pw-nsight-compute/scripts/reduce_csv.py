#!/usr/bin/env python3
"""Reduce Nsight Compute artifacts into normalized ProfileWeaver evidence."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from ncu_artifacts import read_rules, read_source_hotspots, write_drilldowns
from ncu_csv import load, read_metrics, write
from ncu_kernel import choose_bottleneck, reduce_kernel

PROTOCOL = "profileweaver.ncu-diagnostics/v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--capture-metadata", type=Path, required=True)
    parser.add_argument("--details-csv", type=Path)
    parser.add_argument("--source-csv", type=Path)
    parser.add_argument("--timeline-profile", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--phase", choices=["baseline", "variant", "reprofile"], required=True)
    parser.add_argument("--variant")
    parser.add_argument("--run-command", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostics-output", type=Path, required=True)
    parser.add_argument("--max-kernels", type=int, default=2)
    parser.add_argument("--max-stalls", type=int, default=3)
    parser.add_argument("--max-evidence", type=int, default=12)
    parser.add_argument("--drilldown-dir", type=Path)
    parser.add_argument("--max-group-metrics", type=int, default=48)
    args = parser.parse_args()

    capture = load(args.capture_metadata, {})
    export_paths = {item.get("page"): Path(item["path"]) for item in capture.get("exports", [])
                    if item.get("returncode") == 0 and item.get("path")}
    details_path = args.details_csv or export_paths.get("details")
    source_path = args.source_csv or export_paths.get("source")
    rows = read_metrics(args.csv)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["kernel"]].append(row)
    drilldown_dir = args.drilldown_dir or args.diagnostics_output.parent / "ncu-drilldowns"
    drilldowns = write_drilldowns(grouped, drilldown_dir, args.max_group_metrics)
    rules = read_rules(details_path)
    source_hotspots = read_source_hotspots(source_path)
    for group, values in (("rules", rules), ("source", source_hotspots)):
        if not values:
            continue
        path = drilldown_dir / f"{group}.json"
        write(path, {"protocol": "profileweaver.ncu-drilldown/v1", "group": group,
                     "items": values, "context": {"matched": len(values),
                     "selected": len(values), "omitted": 0}})
        drilldowns[group] = {"path": str(path), "matched": len(values),
                             "selected": len(values), "omitted": 0}
    kernels = [reduce_kernel(name, values, args.max_stalls, args.max_evidence)
               for name, values in grouped.items()]
    kernels.sort(key=lambda item: ((item.get("duration") or {}).get("value") or 0), reverse=True)
    omitted = max(0, len(kernels) - args.max_kernels)
    kernels = kernels[:args.max_kernels]
    diagnostics = {
        "protocol": PROTOCOL, "run_id": args.run_id,
        "collection": {"requested": capture.get("requested"), "resolved": capture.get("resolved"),
                       "timing": capture.get("timing"),
                       "report": (capture.get("capture") or {}).get("report")},
        "kernels": kernels,
        "rules": rules,
        "source_hotspots": source_hotspots,
        "drilldowns": drilldowns,
        "context": {"max_kernels": args.max_kernels, "max_stalls_per_kernel": args.max_stalls,
                    "max_evidence_per_kernel": args.max_evidence,
                    "omitted_kernels": omitted, "raw_metric_rows": len(rows),
                    "policy": "consume this capsule; load one named drilldown only when it changes the next action; raw report and full metrics stay under raw/"},
    }
    write(args.diagnostics_output, diagnostics)

    timeline = load(args.timeline_profile, {})
    top = kernels[0] if kernels else {"name": "unknown-kernel"}
    totals = timeline.get("totals") or {"wall_time_s": 0, "gpu_busy_s": None,
                                       "attributed_s": None}
    hotspots = timeline.get("hotspots") or [{
        "rank": 1, "name": top["name"], "kind": "gpu-kernel", "file": None, "line": None,
        "module": None, "self_time_s": 0, "total_time_s": None, "pct_of_wall": 0,
        "calls": None, "samples": None, "callpath": None, "metrics": {}, "amdahl_ceiling": 1,
    }]
    for hotspot in hotspots:
        if hotspot.get("name") == top.get("name") or len(hotspots) == 1:
            hotspot["metrics"] = (hotspot.get("metrics") or {}) | {"ncu": top}
            break
    capture_command = (capture.get("capture") or {}).get("command", "")
    run_command = args.run_command or (timeline.get("workload") or {}).get("run_command") or ""
    profile = {
        "run_id": args.run_id, "phase": args.phase, "variant": args.variant,
        "tool": {"name": "NVIDIA Nsight Compute",
                 "version": (capture.get("tool") or {}).get("version") or "unknown",
                 "mode": "kernel replay/full section set",
                 "command": capture_command, "sampling_hz": None,
                 "events": [item["metric"] for item in rows[:20]], "overhead_factor": None,
                 "multiplexed": None},
        "workload": (timeline.get("workload") or {"run_command": run_command}) | {"run_command": run_command},
        "machine": timeline.get("machine") or {}, "totals": totals, "hotspots": hotspots,
        "bottleneck": choose_bottleneck(top), "kernel_diagnostics": diagnostics,
        "warnings": (timeline.get("warnings") or []) + ([{
            "kind": "missing-timeline",
            "detail": "NCU replay is not an end-to-end timer; join this profile to Nsight Systems before ranking impact",
            "magnitude": None,
        }] if not timeline else []),
        "raw_artifacts": [str(args.csv), str(args.capture_metadata),
                          (capture.get("capture") or {}).get("report")],
    }
    profile["raw_artifacts"] = [item for item in profile["raw_artifacts"] if item]
    write(args.output, profile)
    elapsed = ((capture.get("timing") or {}).get("capture_elapsed_s"))
    print(json.dumps({"status": "complete", "kernels": len(kernels), "metrics": len(rows),
                      "ncu_capture_s": elapsed, "diagnostics": str(args.diagnostics_output),
                      "profile": str(args.output)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, separators=(",", ":")))
        raise SystemExit(2)
