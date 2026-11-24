"""Select a named candidate snapshot's evidence without relabeling baseline data."""

from pathlib import Path

from brief_facts import load
from evidence_scope import requires_roi, roi_scope


def current_evidence(run: Path, state: dict, candidate_id: str | None = None, require_roi: bool | None = None):
    candidates = state.get("candidates") or {}
    if require_roi is None:
        require_roi = requires_roi(load(run / "experiment-plan.json", {}))
    preferred = candidate_id or (state.get("confirmed") or state.get("best") or {}).get("id")
    final = load(run / "profile.reprofile.json", {}) if state.get("confirmed") else {}
    if final.get("phase") == "reprofile" and final.get("variant") == preferred:
        sources = run / "final-profile/source-targets.json"
        targets = load(sources, {}).get("targets") or []
        top = (final.get("hotspots") or [{}])[0]
        matched = next((t for t in targets if t.get("hotspot") == top.get("name")), {})
        return {"id": preferred, "preferred_candidate": preferred, "ancestor_anchor": False,
                "tool": (final.get("tool") or {}).get("name"), "scope": final.get("scope"), "name": top.get("name"),
                "snapshot": "independently-confirmed-winner", "timing_role": "profile-not-ranking",
                "source_targets": str(sources.relative_to(Path(__file__).resolve().parents[2]))
                    if sources.is_relative_to(Path(__file__).resolve().parents[2]) else str(sources),
                "slice": (matched.get("slice") or {}).get("path"),
                "ncu": (state.get("kernel_profiles") or {}).get("final"), "targets": targets}
    order = []
    ancestor = preferred
    while ancestor and ancestor != "baseline" and ancestor not in order:
        order.append(ancestor)
        ancestor = (candidates.get(ancestor) or {}).get("parent")
    if not preferred:
        order = list(reversed(candidates))
    for candidate_id in order:
        meta = candidates.get(candidate_id) or {}
        capsule = meta.get("profile") or {}
        if capsule.get("status") != "complete" or not (meta.get("result") or {}).get("correctness", {}).get("passed"):
            continue
        directory = run / "candidates" / candidate_id / "profile"
        path = Path(capsule.get("profile_artifact") or directory / "profile.json")
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        profile = load(path, {})
        if profile.get("phase") != "variant" or profile.get("variant") not in {None, candidate_id}:
            continue
        if require_roi and not (roi_scope(profile.get("scope")) or roi_scope((capsule.get("acquisition") or {}).get("target_scope"))):
            continue
        targets_path = directory / "source-targets.json"
        targets = load(targets_path, {}).get("targets") or []
        hotspot = ((profile.get("kernel_diagnostics") or {}).get("kernels") or
                   profile.get("hotspots") or [{}])[0]
        name = hotspot.get("name")
        source = next((t for t in targets if t.get("hotspot") == name), {})
        return {"id": candidate_id, "tool": (profile.get("tool") or {}).get("name"),
                "preferred_candidate": preferred, "ancestor_anchor": candidate_id != preferred,
                "scope": profile.get("scope"), "name": name,
                "timing_role": "candidate-profile-not-ranking",
                "snapshot": "measured-candidate", "source_targets": str(targets_path.relative_to(
                    Path(__file__).resolve().parents[2])) if targets_path.is_relative_to(
                        Path(__file__).resolve().parents[2]) else str(targets_path),
                "slice": (source.get("slice") or {}).get("path"),
                "ncu": capsule.get("ncu"),
                "ranking": (meta.get("result") or {}).get("ranking"),
                "targets": targets}
    return None
