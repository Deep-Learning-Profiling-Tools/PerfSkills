"""Use short inspection handles instead of spending the packet on repeated artifact paths."""

from copy import deepcopy


def prepare(view, cid):
    result = deepcopy(view)
    result.pop("detail", None)
    result["detail_relative_to_run"] = f"candidates/{cid}/evidence.json"
    source = result.get("source")
    if source is not None:
        source.setdefault("snapshot", cid)
        source.setdefault("inspect", {"candidate": source["snapshot"]})
        for key in ("slice", "targets", "complete_changes", "control_scope", "target"):
            source.pop(key, None)
    selection = result.get("selection") or {}
    for key in ("id", "selector", "collateral"):
        selection.pop(key, None)
    roi = result.get("roi") or {}
    for key in ("pairs", "provisional", "quality", "confidence", "baseline_reused"):
        roi.pop(key, None)
    if not roi.get("timing_disagreement"):
        roi.pop("timing_disagreement", None)
    for key in ("scope",):
        scope = result.get("observations", {}).get(key)
        if isinstance(scope, dict):
            result["observations"][key] = {k: scope[k] for k in ("policy", "name", "roi_name", "timing_join") if k in scope}
    reference = result.get("observations", {}).get("reference_evidence") or {}
    if reference.get("id"):
        reference["detail"] = f"candidates/{reference['id']}/evidence.json"
    drilldown = result.get("drilldown") or {}
    if isinstance(drilldown, dict):
        result["drilldown"] = drilldown.get("groups", [])
    return result


def minimum(agent):
    """Keep the current outcome and a source lookup even when a literal finding cannot fit."""
    result = {k: agent[k] for k in ("id", "status", "selection", "roi", "decision", "detail_relative_to_run") if k in agent}
    source = agent.get("source")
    if source is not None:
        result["source"] = {k: source[k] for k in ("snapshot", "inspect") if k in source}
        if source.get("changes"):
            result["source"]["changes"] = source["changes"][:1]
    obs = agent.get("observations") or {}
    result["observations"] = {k: obs[k] for k in ("snapshot", "status", "kernel_time_s", "kernel_sum_ms", "provider") if k in obs}
    result["observations"]["detail_status"] = "read-named-detail-for-remaining-evidence"
    return result
