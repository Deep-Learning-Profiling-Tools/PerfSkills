"""Estimate the value of learning from the current branch, using measured progress."""

import math
from experiment_support import REPO, load


def refined(plan):
    return (plan.get("profiling", {}).get("skill_runtime") or {}).get("decision_policy") == "budgeted-lookahead-v3"


def recent(state, row=None):
    rows = [(m.get("result") or {}) for m in state.get("candidates", {}).values() if m.get("result")]
    if row:
        rows = [r for r in rows if r.get("id") != row.get("id")] + [row]
    return rows[-5:]


def misses(state, row, threshold=1.02):
    count = 0
    for sample in reversed(recent(state, row)):
        if not (sample.get("correctness") or {}).get("passed"):
            break
        rank = sample.get("ranking") or {}
        point = (sample.get("improvement") or {}).get("point")
        if point is None or point >= max(1., rank.get("incumbent_at_screen") or 1.) * threshold:
            break
        count += 1
    return count


def opportunity(plan, run, state, target):
    """A timing fraction is an Amdahl removal bound, never a Roofline prediction."""
    meta = (state.get("candidates") or {}).get(target) or {}
    artifact = (meta.get("profile") or {}).get("profile_artifact")
    path = REPO / artifact if artifact else run / "profile.baseline.json"
    profile = load(path, {})
    wall = (profile.get("totals") or {}).get("wall_time_s")
    top = (profile.get("hotspots") or [{}])[0]
    duration = top.get("self_time_s")
    if type(wall) in (int, float) and wall > 0 and type(duration) in (int, float):
        ratio = duration / wall
        if 0 <= ratio <= 1:
            return ratio
    return None


def gains(plan, state, rows):
    from decision_learning import incumbent_gain
    from selection_objective import policy_row
    rows = [policy_row(state, row) for row in rows]
    prior = math.log(max(1.001, plan.get("sampling", {}).get("practical_threshold", 1.02)))
    observed = [g for row in rows if (g := incumbent_gain(row)) is not None]
    weights = [2. ** (i-len(observed)+1) for i in range(len(observed))]
    estimate = (prior + sum(w*g for w,g in zip(weights, observed))) / (1+sum(weights))
    positives = [g for g in observed if g > 0]
    return {"edit_gain": estimate, "win_magnitude": max([prior] + positives),
            "stable_screens": len(observed), "positive_screens": len(positives),
            "progress_basis": "recent-decayed-correct-observations" if observed else "cold-prior"}
