"""Acquire only missing kernel timing for an explicit selection objective."""

from adaptive_policy import enabled
from experiment_support import load
from experiment_state import save_state
from selection_objective import TOOLS, kernel_mode, make, observation, reconcile, summary


def initialize(plan, run, state, args):
    config = plan.get("selection_objective") or {}
    scope = getattr(args, "objective", None) or config.get("scope")
    if not scope:
        return
    if not enabled(plan):
        if not getattr(args, "objective", None):
            return
        raise ValueError("an explicit selection objective requires adaptive mode")
    requested = getattr(args, "kernel", None) or config.get("kernel_regex")
    if scope == "kernel" and not requested:
        raise ValueError("--objective kernel requires --kernel REGEX")
    provider = (getattr(args, "timing_provider", None) or config.get("provider") or
                (state.get("selection_objective") or {}).get("provider"))
    doc = load(run / "profile.baseline.json", {})
    provider = provider or TOOLS.get((doc.get("tool") or {}).get("name"))
    from objective_comparison import configuration
    spec = configuration(plan, make(scope, requested, provider))
    if scope == "kernel":
        from selection_objective import anchor_baseline
        anchor_baseline(run, state)
    old = state.get("selection_objective")
    if old and old != spec:
        raise ValueError("use the objective command to revise an existing selection objective")
    state["selection_objective"] = spec
    reconcile(plan, run, state)
    save_state(run, state)


def ensure(plan, run, state, baseline, cid):
    if not kernel_mode(state):
        return
    from evidence_acquisition import acquire, validate_snapshot
    from evidence_scope import requires_roi
    spec = state["selection_objective"]
    for name in ("baseline", cid):
        if name != "baseline":
            validate_snapshot(run, state, name)
        if observation(plan, run, state, name) is None:
            needs = ["gpu-hotspot"] + (["roi-attribution"] if requires_roi(plan) else [])
            acquire(plan, run, state, baseline, name, needs, spec["provider"])
    reconcile(plan, run, state)
    save_state(run, state)


def request(state, cid, acquisition, automatic, run=None):
    if run is not None and automatic and acquisition.get("reason") == "roi-wall-disagreement":
        from activity_context import reusable
        previous = reusable(run,state,cid)
        if previous:
            return {"tool":"none","reason":"primary-kernel-timing-already-observed",
                    "historical_context":previous,"action":"reuse-historical-context","screening_only":True}
    if (kernel_mode(state) and automatic and acquisition.get("status") != "blocked" and
            not (set(acquisition.get("needs") or []) - {"gpu-hotspot", "roi-attribution", "transfer"}) and
            acquisition.get("tool") in {"none", "activity", "timeline"} and
            (state["candidates"][cid].get("objective_result") or {}).get("status") == "measured" and
            acquisition.get("reason") != "roi-wall-disagreement"):
        return {"tool": "none", "reason": "primary-kernel-timing-already-observed", "screening_only": True,
                **{k: acquisition[k] for k in ("action", "historical_fact", "historical_context") if k in acquisition},
                "policy_reason": acquisition.get("reason")}
    return acquisition


def select(plan, run, state, *, scope, pattern=None, provider=None, candidate=None, comparison_provider=None):
    """Revise selection explicitly; original benchmark plans and samples remain attributable."""
    from experiment_report import refresh_brief
    if not enabled(plan):
        raise ValueError("objective selection requires adaptive mode")
    if candidate:
        meta = state["candidates"].get(candidate)
        if not meta or not pattern or scope != "kernel" or not kernel_mode(state):
            raise ValueError("a renamed target requires an active kernel objective and a known candidate")
        import re
        re.compile(pattern)
        meta["kernel_selector"] = pattern
    else:
        doc = load(run / "profile.baseline.json", {})
        provider = provider or TOOLS.get((doc.get("tool") or {}).get("name"))
        from objective_comparison import configuration
        spec = configuration(plan, make(scope, pattern, provider), comparison_provider)
        old = state.get("selection_objective")
        if old != spec:
            from experiment_support import write
            prior = load(run / "measurement-verdict.json", {})
            if prior:
                write(run / "selection-history" / (str((old or {}).get("id", "application")) + ".json"), prior)
            state["selection_objective"] = spec
    state.pop("selected", None)
    state.pop("confirmed", None)
    if scope == "application":
        from measured_checkpoint import select_best
        state.pop("best", None)
        for cid, meta in state["candidates"].items():
            if meta.get("result"):
                select_best(plan, state, cid, meta["result"])
    else:
        from selection_objective import anchor_baseline
        anchor_baseline(run, state)
        from evidence_acquisition import validate_snapshot
        for cid, meta in state["candidates"].items():
            if meta.get("result"):
                validate_snapshot(run, state, cid)
        reconcile(plan, run, state)
    save_state(run, state)
    refresh_brief(plan, run, state)
    return {"status": "objective-updated", "objective": summary(state), "leader": state.get("best"),
            "missing_timing": [cid for cid, m in state["candidates"].items() if m.get("result")
                               and kernel_mode(state) and m.get("objective_result", {}).get("status") != "measured"],
            "new_benchmark_runs": 0, "new_profiling_runs": 0}
