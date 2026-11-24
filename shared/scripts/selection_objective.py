"""Bind kernel selection to comparable timing captures while preserving application samples."""

import math
import re
from capture_history import records
from evidence_scope import requires_roi, roi_scope
from experiment_support import load
from skill_binding import digest

TOOLS = {"cupti-activity": "pw-cupti", "nsight-systems": "pw-nsight-systems",
         "HPCToolkit": "pw-hpctoolkit"}


def specification(state):
    return state.get("selection_objective") or {}


def kernel_mode(state):
    return specification(state).get("scope") == "kernel"


def candidate_selector(state, parent, pattern):
    pattern = pattern or (state.get("candidates", {}).get(parent) or {}).get("kernel_selector")
    if pattern:
        re.compile(pattern)
    return {"kernel_selector": pattern} if pattern else {}


def selector(state, cid):
    seen = set()
    while cid not in seen and cid in state.get("candidates", {}):
        seen.add(cid)
        meta = state["candidates"][cid]
        if meta.get("kernel_selector"):
            return meta["kernel_selector"]
        cid = meta.get("parent")
    return specification(state).get("kernel")


def scope_key(scope):
    if not isinstance(scope, dict):
        return scope
    return [scope.get("policy"), scope.get("name") or scope.get("roi_name")]


def observation(plan, run, state, cid):
    spec = specification(state)
    identity = state.get("contract_sha256") if cid == "baseline" else (
        state.get("candidates", {}).get(cid) or {}).get("measurement_fingerprint")
    documents = [(load(path, {}), path) for _, path in records(run, cid, identity)] if identity else []
    # Initialization already produced this baseline capture under the source contract.
    if cid == "baseline":
        documents.append((load(run / "profile.baseline.json", {}), run / "profile.baseline.json"))
    pattern = re.compile(selector(state, cid) or spec["kernel"])
    for doc, path in documents:
        tool = (doc.get("tool") or {}).get("name")
        if TOOLS.get(tool, tool) != spec["provider"] or (requires_roi(plan) and not roi_scope(doc.get("scope"))):
            continue
        if cid != "baseline" and doc.get("variant") != cid:
            continue
        matches = [h for h in doc.get("hotspots", []) if pattern.search(h.get("name", ""))]
        seconds = [h.get("self_time_s") for h in matches]
        if seconds and all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in seconds) and math.isfinite(sum(seconds)):
            return {"seconds": sum(seconds), "kernels": [h["name"] for h in matches],
                    "calls": sum(h.get("calls") or 0 for h in matches), "artifact": str(path),
                    "provider": spec["provider"], "scope": scope_key(doc.get("scope")),
                    "fingerprint": identity, "measurement": "profiled-kernel-sum"}


def anchor_baseline(run, state):
    from capture_history import preserve
    from experiment_support import sha256
    path = run / "profile.baseline.json"
    identity = state.get("contract_sha256")
    if identity and path.is_file() and not any(e["sha256"] == sha256(path) for e,_ in records(run,"baseline",identity)):
        preserve(run,"baseline",{"status":"complete","profile_artifact":str(path)},identity)


def reconcile(plan, run, state):
    """Refresh the leader from correct snapshots using one provider and timing scope."""
    if not kernel_mode(state):
        return
    spec = specification(state)
    baseline = observation(plan, run, state, "baseline")
    state["objective_baseline"] = baseline
    best, point = None, 1.
    for cid, meta in state.get("candidates", {}).items():
        row = meta.get("result") or {}
        if not (row.get("correctness") or {}).get("passed"):
            continue
        current = observation(plan, run, state, cid)
        valid = bool(current and baseline and current["scope"] == baseline["scope"])
        meta["objective_result"] = dict(current or {}, status="measured" if valid else "unresolved",
            objective_id=spec["id"], speedup=baseline["seconds"]/current["seconds"] if valid else None,
            incumbent_at_screen=point,
            confidence_interval=None, statistical_significance="not-established")
        if valid and meta["objective_result"]["speedup"] > point:
            best, point = cid, meta["objective_result"]["speedup"]
    state["best"] = ({"id": best, "improvement": point, "workspace": state["candidates"][best]["workspace"],
                      "objective_id": spec["id"]} if best else None)


def policy_row(state, row):
    if not kernel_mode(state):
        return row
    obs = state["candidates"][row["id"]].get("objective_result") or {}
    if obs.get("status") != "measured":
        return row | {"ranking": {"status": "invalid", "incumbent_at_screen": 1.},
                      "improvement": {}, "selection_unresolved": True}
    base = state["objective_baseline"]["seconds"]
    return row | {"metric": {"median": obs["seconds"]}, "baseline_metric": {"median": base},
                  "improvement": {"point": obs["speedup"], "ci95": None},
                  "ranking": {"status": "single-capture", "incumbent_at_screen": obs["incumbent_at_screen"]},
                  "measurement_role": "profiled-kernel-selection"}


def summary(state):
    spec = specification(state)
    return ({"scope": "kernel", "selector": spec["kernel"], "provider": spec["provider"],
             "id": spec["id"], "measurement": "profiled-kernel-sum",
             "collateral":spec.get("collateral","unprofiled")}
            if kernel_mode(state) else {"scope": "application"})


def notice(state):
    if not kernel_mode(state):
        return {}
    return {"objective": summary(state), "winner": (state.get("best") or {}).get("id", "baseline"),
            "improvement": (state.get("best") or {}).get("improvement", 1.)}


def make(scope, pattern=None, provider=None):
    if scope == "application":
        return {"scope": scope, "id": digest([scope])}
    if not pattern or not provider:
        raise ValueError("kernel objective requires a kernel selector and capable timing provider")
    re.compile(pattern)
    if provider not in TOOLS.values():
        raise ValueError("kernel selection requires CUPTI activity, NSYS or HPCToolkit timing")
    return {"scope": scope, "kernel": pattern, "provider": provider, "id": digest([scope, pattern, provider])}
