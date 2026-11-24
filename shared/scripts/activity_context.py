"""Reuse recent host/device context only when current activity shows the same graph."""

from capture_history import records
from experiment_support import load
from selection_objective import scope_key


def activity(run, state, cid):
    meta = state.get("candidates", {}).get(cid) or {}
    identity = meta.get("measurement_fingerprint")
    if not identity:
        return None
    for record,path in records(run,cid,identity):
        if record.get("provider") != "pw-cupti":
            continue
        summary = record.get("summary") or {}
        values = [summary.get(k) for k in ("launches","copy_calls","copy_bytes")]
        profile = load(path,{})
        kernels = [(h.get("name"),h.get("calls")) for h in profile.get("hotspots",[])]
        if kernels and all(type(v) is int for v in values):
            return [scope_key(profile.get("scope")),values,sorted(kernels)]


def reusable(run, state, cid):
    graph = activity(run,state,cid)
    current = ((state["candidates"][cid].get("result") or {}).get("wall_improvement") or {}).get("point")
    if graph is None or not current:
        return None
    previous = list(state["candidates"])
    previous = previous[:previous.index(cid)]
    for prior_id in reversed(previous[-2:]):
        meta = state["candidates"][prior_id]
        before = ((meta.get("result") or {}).get("wall_improvement") or {}).get("point")
        if not before or current < before or graph != activity(run,state,prior_id):
            continue
        for record,path in records(run,prior_id,meta.get("measurement_fingerprint")):
            if {"sync","launch-overhead"} <= set(record.get("capabilities") or []):
                return {"candidate":prior_id,"artifact":str(path),"role":"historical-context",
                        "basis":"same-activity-graph-and-no-worse-collateral-sample"}
    return None
