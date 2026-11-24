"""Describe current profile observations and their comparison scope."""

from pathlib import Path
from profile_policy import timeline_for_candidate
from experiment_support import REPO, display


def timeline_observations(profile, details):
    """Credit only dimensions actually present in a reduced NSYS capture."""
    caps = []
    if (profile.get("tool") or {}).get("name") != "nsight-systems":
        return caps
    if profile.get("hotspots"):
        caps.append("gpu-hotspot")
    if (details.get("roi") or {}).get("wall_s") is not None:
        caps.append("roi-attribution")
    if "host_cuda_api" in details:
        caps.append("sync")
        if any("launch" in name.lower() for name in details["host_cuda_api"]):
            caps.append("launch-overhead")
    if (details.get("gpu") or {}).get("idle_s") is not None:
        caps.append("gpu-idle")
    if "copies" in (details.get("scope") or {}):
        caps.append("transfer")
    return caps


def timeline_context(details, needs):
    """Expose the host API evidence requested by the question, with its original units."""
    result = {}
    if "sync" in needs and isinstance(details.get("host_cuda_api"), dict):
        sync = [(name, row) for name, row in details["host_cuda_api"].items()
                if "synchron" in name.lower() or "wait" in name.lower()]
        result["sync_calls"] = sum(row["calls"] for _, row in sync)
        if sync:
            name, row = max(sync, key=lambda item: item[1].get("thread_time_s", 0))
            result["leading_sync_api"] = {"name": name, **{k: row[k] for k in
                ("calls", "thread_time_s", "gpu_idle_overlap_s") if k in row}}
    if "launch-overhead" in needs:
        launches = [(name, row) for name, row in (details.get("host_cuda_api") or {}).items()
                    if "launch" in name.lower()]
        if launches:
            name, row = max(launches, key=lambda item: item[1].get("thread_time_s", 0))
            result["leading_launch_api"] = {"name": name, **{k: row[k] for k in
                ("calls", "thread_time_s", "gpu_idle_overlap_s") if k in row}}
        if (details.get("gpu") or {}).get("kernel_launches") is not None:
            result["kernel_launches"] = details["gpu"]["kernel_launches"]
    return result


def observation_capsule(candidate_id, profile, sources, profile_path, diff_path):
    """A changed provider contributes current facts, never cross-tool speedups."""
    top = (profile.get("hotspots") or [{}])[0]
    target = next((t for t in sources.get("targets", []) if t.get("hotspot") == top.get("name")), {})
    return {"id": candidate_id, "status": "complete", "cache": "miss", "scope": profile.get("scope"),
            "profile_artifact": str(profile_path), "artifact": display(diff_path),
            "comparison_scope": "different-provider-observations-only",
            "profiled_speedup": None,
            "target": {"name": top.get("name")},
            "next": {"name": top.get("name"), "wall_pct": top.get("pct_of_wall"),
                     "slice": (target.get("slice") or {}).get("path")}}


def comparison_baseline(plan, run, variables, ncu_enabled, adaptive=False):
    path = (run / "profile.ncu.baseline.json" if ncu_enabled and not timeline_for_candidate(plan)
            else Path(variables["baseline_profile"]))
    # An on-demand candidate replay does not require a second baseline replay.
    # Its own counters are useful even when cross-provider deltas are unavailable.
    return Path(variables["baseline_profile"]) if adaptive and not path.is_file() else path


def _artifact_path(value: dict) -> Path:
    path = Path(value.get("source_artifact") or value["artifact"])
    return path if path.is_absolute() else REPO / path


def _candidate_ncu_enabled(plan: dict, phase: str, force: bool) -> bool:
    if not (plan.get("kernel_profile") or {}).get("enabled"):
        return False
    if force or phase == "confirm":
        return True
    return (plan.get("sampling") or {}).get("mode") != "winner-confirmation"

