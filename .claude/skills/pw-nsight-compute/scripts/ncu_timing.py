"""Join replay counters only to timing for the same candidate and kernel.

An older timeline can supply workload context, never current application time.
Replay duration remains in ncu.duration and is not an ROI measurement.
"""

from copy import deepcopy


def join(timeline: dict, kernels: list[dict], phase: str, variant: str | None, run_id=None):
    same_scope = (bool(timeline) and timeline.get("phase") == phase and timeline.get("variant") == variant
                  and (run_id is None or timeline.get("run_id") == run_id))
    observed = {h.get("name"): h for h in timeline.get("hotspots", [])}
    names_match = bool(kernels) and all(k["name"] in observed for k in kernels)
    fresh = same_scope and names_match
    totals = (deepcopy(timeline["totals"]) if fresh and timeline.get("totals") else
              {"wall_time_s": None, "gpu_busy_s": None, "attributed_s": None})
    # Preserve the timeline ranking when it is genuinely current; otherwise expose
    # only the captured symbols, ranked by replay duration, with unknown ROI cost.
    hotspots = deepcopy(timeline.get("hotspots", [])) if fresh else []
    for rank, kernel in enumerate(kernels, 1):
        hotspot = next((h for h in hotspots if h.get("name") == kernel["name"]), None)
        if hotspot is None:
            hotspot = {"rank": rank, "name": kernel["name"], "kind": "gpu-kernel",
                       "file": None, "line": None, "module": None,
                       "self_time_s": None, "total_time_s": None, "pct_of_wall": None,
                       "calls": None, "samples": None, "callpath": None,
                       "metrics": {}, "amdahl_ceiling": None}
            hotspots.append(hotspot)
        hotspot["metrics"] = (hotspot.get("metrics") or {}) | {"ncu": kernel}
    provenance = {"timing_join": "same-candidate-and-symbol" if fresh else "context-only",
                  "timeline_phase": timeline.get("phase"), "timeline_variant": timeline.get("variant"),
                  "counter_phase": phase, "counter_variant": variant,
                  "ranking": "timeline-time" if fresh else "replay-duration-not-roi"}
    return totals, hotspots, provenance
