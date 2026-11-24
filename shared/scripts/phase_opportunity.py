"""Prioritize measured work across an execution region, with scoped evidence gaps."""

import re

from application_phases import from_sample
from evidence_scope import requires_roi, roi_scope
from experiment_support import load
from performance_models import removal_ceiling
from timeline_summary import kernel_families


def _application_phase(run, plan, sample):
    metric = plan.get("metric") or {}
    wall = sample.get("wall_s") or 0
    if not wall or (metric.get("source") != "wall" and metric.get("scope") not in {"whole-process", "end-to-end"}):
        return None
    leading = next((row for row in from_sample(run, sample) if wall / 4 <= row["seconds"] <= wall), None)
    if leading:
        share = leading["seconds"] / wall
        return leading | {"kind": "application-phase", "basis": "unprofiled-process",
                          "share": share, "amdahl_removal_ceiling": removal_ceiling(share),
                          "action": "inspect-phase-source"}
    return None


def _runtime_work(details, wall):
    apis = []
    for name, row in (details.get("host_cuda_api") or {}).items():
        if not re.search(r"^(?:cuda|cu)(?:mem(?:alloc|free|cpy|set)|malloc|free|.*synchron|launch)", name, re.I):
            continue
        if (row.get("calls") or 0) >= 20 and row.get("gpu_idle_overlap_s") is not None:
            apis.append({"name": name, "calls": row["calls"], "idle_overlap_s": row["gpu_idle_overlap_s"]})
    apis.sort(key=lambda row: row["idle_overlap_s"], reverse=True)
    if apis and apis[0]["idle_overlap_s"] >= .05 * wall:
        copies = ((details.get("scope") or {}).get("copies") or {}).get("inside_roi") or {}
        roi = details.get("roi") or {}
        return {"kind": "repeated-operations", "basis": "one-API-idle-overlap",
                "seconds": apis[0]["idle_overlap_s"], "apis": apis[:3],
                "copies": copies.get("events"), "copy_union_s": copies.get("union_s"),
                "roi_window_ns": [roi.get("start_ns"), roi.get("end_ns")],
                "action": "inspect-shared-caller"}
    return None


def _handles(targets, names):
    return [t["id"] for t in targets if not t.get("dependency") and (t.get("hotspot") in names or
            any(h.get("hotspot") in names for h in t.get("additional_hotspots", [])))][:3]


def rank(run, plan, profile, details, targets, sample):
    application = _application_phase(run, plan, sample)
    if application:
        return application
    totals = profile.get("totals") or {}
    wall, busy = totals.get("wall_time_s") or 0, totals.get("gpu_busy_s")
    top = next((h for h in profile.get("hotspots", []) if h.get("kind") == "gpu-kernel"), {})
    top_s = top.get("self_time_s") or 0
    if not wall or not top_s:
        return {}
    tool = (profile.get("tool") or {}).get("name")
    scope = profile.get("scope") or {}
    in_roi = roi_scope(scope)
    if requires_roi(plan) and not in_roi:
        return {}
    runtime = _runtime_work(details, wall) if tool == "nsight-systems" and in_roi else None
    if runtime and runtime["seconds"] >= top_s:
        return runtime | {"targets": _handles(targets, {top.get("name")})}
    alternative = {"host_runtime": runtime} if runtime else {}
    if tool == "cupti-activity" and in_roi and busy is not None and 0 <= busy < wall / 2:
        return {"kind": "unattributed-gap", "basis": "wall-minus-gpu-busy-union", "seconds": wall - busy,
                "action": "request-host-timeline", "needs": ["sync", "launch-overhead", "roi-attribution"]}
    rows = kernel_families(profile, wall)
    family = next((row for row in rows if row["members"] >= 2 and row["time_s"] >= 1.15 * top_s), None)
    if family:
        return {"kind": "kernel-family", "basis": "kernel-duration-sum", "seconds": family["time_s"],
                "family": family["name"], "calls": family["calls"], "members": family["members"],
                "targets": _handles(targets, set(family["hotspots"])), "action": "compare-family-rewrites"} | alternative
    ceiling = removal_ceiling(top_s / wall) if in_roi and top.get("calls") == 1 else top.get("amdahl_ceiling")
    return {"kind": "kernel", "basis": "kernel-duration-sum", "seconds": top_s,
            "amdahl_removal_ceiling": ceiling, "action": "inspect-hotspot"} | alternative


def current(run, plan, state, snapshot="baseline"):
    """Use only evidence belonging to the requested snapshot."""
    from brief_facts import resolve_artifact

    baseline = snapshot == "baseline"
    meta = (state.get("candidates") or {}).get(snapshot) or {}
    samples = ((meta.get("result") or {}).get("samples") or {}).get("candidate") or [{}]
    sample = (load(run / "baseline-timing.json", {}).get("sample") or {}) if baseline else samples[0]
    application = _application_phase(run, plan, sample)
    if application:
        return application | {"snapshot": snapshot}
    capsule = (state.get("baseline_evidence") if baseline else meta.get("profile")) or {}
    path = resolve_artifact(run, capsule.get("profile_artifact")) if capsule.get("status") == "complete" else None
    profile = load(path, {}) if path else {}
    profiled_snapshot = profile.get("variant") if profile.get("phase") == "variant" else profile.get("phase")
    focused = (profile.get("tool") or {}).get("name") not in {"nsight-systems", "cupti-activity"}
    if profiled_snapshot != snapshot or focused:
        if not baseline:
            return {}
        profile = load(run / "profile.baseline.json", {})
        path = run / "profile.baseline.json"
    details = {}
    if (profile.get("tool") or {}).get("name") == "nsight-systems":
        for artifact in profile.get("raw_artifacts") or []:
            resolved = resolve_artifact(run, artifact)
            if resolved and resolved.name.endswith("metrics.json"):
                details = load(resolved, {})
                break
        if not details and baseline and path == run / "profile.baseline.json":
            details = load(run / "roi-metrics.json", {})
    targets = load((run if path == run / "profile.baseline.json" else run / "candidates" / snapshot / "profile")
                   / "source-targets.json", {}).get("targets", [])
    result = rank(run, plan, profile, details, targets, {})
    if result.get("host_runtime") or result.get("kind") == "repeated-operations":
        for artifact in profile.get("raw_artifacts") or []:
            resolved = resolve_artifact(run, artifact)
            if resolved and resolved.suffix == ".sqlite" and resolved.resolve().is_relative_to(run.resolve()):
                result["trace_artifact"] = str(resolved)
                break
    return result | {"snapshot": snapshot} if result else {}


def compact(opportunity):
    item = opportunity
    kind = item.get("kind")
    snapshot = item.get("snapshot", "baseline")
    command = "inspect --section source --group phase" + (f" --candidate {snapshot}" if snapshot != "baseline" else "")
    host = item.get("host_runtime")
    if host:
        api = host["apis"][0]
        host_line = (f"{api['name']} {api['calls']:,} calls, {host['seconds']:.3g}s GPU-idle overlap. "
                     f"Inspect its callers via {command}.")
    if kind == "application-phase":
        detail = f"{item['phase']} {item['seconds']:.4g}s unprofiled ({item['share']:.0%} of process)."
        if item["amdahl_removal_ceiling"]:
            detail += f" Removal ceiling {item['amdahl_removal_ceiling']:.2f}x."
    elif kind == "repeated-operations":
        api = item["apis"][0]
        detail = f"{api['name']} {api['calls']:,} calls, {item['seconds']:.3g}s GPU-idle overlap."
        if item["copies"]:
            detail += f" {item['copies']:,} in-region copies."
    elif kind == "kernel-family":
        where = ",".join(item["targets"])
        detail = f"{item['family']} spans {item['members']} kernels, {item['seconds']:.3g}s summed."
        follow = host_line if host else f"Check shared work in {where or 'that family'} and its caller via {command}."
        return f"PHASE {snapshot}: {detail} {follow}"
    elif kind == "unattributed-gap":
        return (f"PHASE {snapshot}: {item['seconds']:.3g}s outside GPU busy union. "
                f"Resolve host overlap with evidence {snapshot} --need sync --need launch-overhead --need roi-attribution.")
    elif kind == "kernel" and host:
        return f"PHASE {snapshot}: {host_line}"
    elif kind == "kernel" and item.get("amdahl_removal_ceiling"):
        return f"BOUND {snapshot}: Amdahl hotspot-removal ceiling {item['amdahl_removal_ceiling']:.3g}x for the profiled region."
    else:
        return None
    return f"PHASE {snapshot}: {detail} Inspect the related source via {command}."
