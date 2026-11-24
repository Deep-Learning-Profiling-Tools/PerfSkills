"""Describe measured candidate progress separately from a successful evaluation."""

import math
from selection_objective import kernel_mode


def ratio(state, cid):
    if cid == "baseline":
        return 1.
    meta = state.get("candidates", {}).get(cid) or {}
    result = meta.get("result") or {}
    if not (result.get("correctness") or {}).get("passed"):
        return None
    if kernel_mode(state):
        measured = meta.get("objective_result") or {}
        value = measured.get("speedup") if measured.get("status") == "measured" else None
    else:
        value = (result.get("improvement") or {}).get("point")
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def describe(plan, state, cid):
    meta = state.get("candidates", {}).get(cid) or {}
    row = meta.get("result") or {}
    primary = meta.get("objective_result") if kernel_mode(state) else row.get("ranking")
    before = (primary or {}).get("incumbent_at_screen")
    if before is None:
        earlier = list(state.get("candidates", {}))
        earlier = earlier[:earlier.index(cid)] if cid in earlier else []
        before = max([1.] + [value for key in earlier if (value := ratio(state, key)) is not None])
    value, parent = ratio(state, cid), ratio(state, meta.get("parent"))
    margin = max(0., plan.get("sampling", {}).get("practical_threshold", 1.02) - 1.)
    correct = (row.get("correctness") or {}).get("passed")
    quality = "single-capture" if kernel_mode(state) and value is not None else (row.get("ranking") or {}).get("status")
    status = ("incorrect" if correct is False else "unresolved" if value is None else
              "provisional" if quality in {"unstable", "invalid", "resolution-limited"} else
              "regression" if value < before / (1 + margin) else
              "observed-improvement" if value > before * (1 + margin) else "no-clear-change")
    return {"status": status, "retained": (state.get("best") or {}).get("id", "baseline"),
            "baseline_speedup": value, "parent_speedup": value / parent if value and parent else None,
            "incumbent_speedup": value / before if value and before else None,
            "significance": "not-established" if row.get("confidence") != "independent-final-pairs" else "see-final-gates"}


def terminal(plan, state, cid, evaluation_passed):
    """Bounded screens use acceptance for an observed improvement, never correctness alone."""
    if plan.get("sampling", {}).get("measurement_policy") != "single-or-two-v3":
        return {}
    result = describe(plan, state, cid)
    return {"evaluation_passed": evaluation_passed,
            "accepted": evaluation_passed and result["status"] == "observed-improvement",
            "candidate_result": result}
