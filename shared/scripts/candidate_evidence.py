"""Resolve one explicitly named snapshot; never use baseline as its variant."""

from pathlib import Path

from experiment_support import REPO, load


def profile_for(run: Path, candidate: str):
    if candidate == "baseline":
        path = run / "profile.baseline.json"
    elif candidate == "final":
        path = run / "profile.reprofile.json"
    else:
        state = load(run / "experiment-state.json", {})
        meta = (state.get("candidates") or {}).get(candidate)
        if meta is None:
            raise ValueError("unknown candidate: " + candidate)
        capsule = meta.get("profile") or {}
        directory = run / "candidates" / candidate / "profile"
        path = Path(capsule.get("profile_artifact") or directory / "profile.json")
        if not path.is_absolute():
            path = REPO / path
        if not path.is_file() and (directory / "profile.activity.json").is_file():
            path = directory / "profile.activity.json"
    profile = load(path, {})
    if candidate not in {"baseline", "final"}:
        if profile.get("phase") != "variant" or profile.get("variant") not in {None, candidate}:
            return {}, path
        # Archived counter-only packets may contain the old baseline-join bug.
        # Expose captured symbols and unknown times without editing the archive.
        kernels = (profile.get("kernel_diagnostics") or {}).get("kernels") or []
        if kernels and any(k["name"] not in {h["name"] for h in profile.get("hotspots", [])} for k in kernels):
            profile = profile | {"totals": {"wall_time_s": None, "gpu_busy_s": None},
                "hotspots": [{"rank": n, "name": k["name"], "kind": "gpu-kernel", "self_time_s": None,
                              "pct_of_wall": None, "metrics": {"ncu": k}} for n, k in enumerate(kernels, 1)],
                "scope": {"timing_join": "historical-attribution-corrected-on-read"}}
    return profile, path


def compact_hotspots(profile: dict, limit: int):
    keys = ("rank", "name", "kind", "file", "line", "self_time_s", "pct_of_wall", "calls", "callpath")
    return [{k: h[k] for k in keys if k in h} for h in profile.get("hotspots", [])[:limit]]
