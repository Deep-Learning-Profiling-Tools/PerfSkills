"""Compare a selected kernel with a requested reference profiler using cached captures."""

from selection_objective import make, observation
from evidence_scope import requires_roi


def compare(plan, run, state, baseline, winner):
    provider = state["selection_objective"].get("comparison_provider")
    if not provider:
        return None
    from evidence_acquisition import acquire
    temporary = state | {"selection_objective": dict(state["selection_objective"], provider=provider)}
    values = {}
    for cid in dict.fromkeys(("baseline", winner)):
        current = observation(plan, run, temporary, cid)
        if current is None:
            needs = ["gpu-hotspot"] + (["roi-attribution"] if requires_roi(plan) else [])
            result = acquire(plan, run, state, baseline, cid, needs, provider)
            current = observation(plan, run, temporary, cid) if result.get("status") == "complete" else None
        values[cid] = current
    before, after = values["baseline"], values[winner]
    valid = bool(before and after and before["scope"] == after["scope"])
    return {"provider": provider, "status": "measured" if valid else "unresolved",
            "baseline": before, "candidate": after,
            "speedup": before["seconds"]/after["seconds"] if valid else None,
            "confidence_interval": None, "statistical_significance": "not-established"}


def configuration(plan, spec, requested=None):
    collateral = (plan.get("selection_objective") or {}).get("collateral")
    if collateral and spec["scope"] == "kernel":
        spec["collateral"] = collateral
    value = requested or (plan.get("selection_objective") or {}).get("comparison_provider")
    if value and spec["scope"] == "kernel":
        make("kernel", spec["kernel"], value)
        spec["comparison_provider"] = value
    return spec
