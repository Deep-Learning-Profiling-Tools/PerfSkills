"""Join current timing and source with separately labeled reference mechanism evidence."""

from compiler_evidence import for_target
from experiment_support import load
from screen_outcome import describe
from selection_objective import kernel_mode


def complete(plan, run, state, cid, view, source_path, targets, *, timeline=None):
    if not targets:
        from changed_sources import ensure
        targets = ensure(run, state, cid, (view.get("source") or {}).get("changes") or [])
    meta = state.get("candidates", {}).get(cid) or {}
    measured = meta.get("objective_result") or {}
    obs = view["observations"]
    if timeline:
        needs = (view.get("acquisition") or {}).get("needs") or []
        fields = ({"sync_calls", "leading_sync_api"} if "sync" in needs else set())
        if "launch-overhead" in needs:
            fields.update(("kernel_launches", "leading_launch_api"))
        if fields & timeline.keys():
            obs["timeline"] = {k: v for k, v in timeline.items() if k in fields}
    if kernel_mode(state) and measured.get("status") == "measured":
        names = measured.get("kernels") or []
        # Keep another snapshot's counters under reference_evidence. This timing
        # belongs to the candidate's immutable primary capture, even on reuse.
        reference = obs.get("reference_evidence")
        if obs.get("status") == "no-new-capture":
            obs = {"snapshot": cid, "status": "measured-primary-reference-mechanism" if reference else "measured-primary"}
            scope = measured.get("scope")
            if isinstance(scope, list) and len(scope) == 2:
                obs["scope"] = {"policy": scope[0], "name": scope[1]}
            if reference:
                obs["reference_evidence"] = reference
            view["observations"] = obs
            view["unknowns"] = [s for s in view["unknowns"] if s != "No new dynamic evidence for this evaluation."]
        if not obs.get("target") and names:
            obs["target"] = names[0]
        if obs.get("status") in {"measured-primary", "measured-primary-reference-mechanism"}:
            obs["kernel_sum_ms"] = measured["seconds"] * 1000 if measured.get("seconds") is not None else None
            obs["provider"] = state["selection_objective"]["provider"]
        if not (view.get("static") or {}).get("registers") and names:
            resources = for_target(load(run / "compiler" / (cid + ".json"), {}), names[0])
            actual = (resources.get("kernels") or [{}])[0]
            if actual:
                view["static"] = {"target": names[0], **{k: actual[k] for k in
                    ("registers", "local_bytes", "shared_bytes", "stack_bytes") if k in actual}}
                view["unknowns"] = [s for s in view["unknowns"] if s != "Static resources for the observed target are unresolved."]
    source = view.setdefault("source", {})
    source.pop("slice", None)
    source.update(snapshot=cid, targets=str(source_path))
    match = next((t for t in targets if t.get("hotspot") == obs.get("target")), None)
    if match:
        source["slice"] = (match.get("slice") or {}).get("path")
        source["target"] = match.get("id")
    elif targets:
        source.pop("slice", None)
        source["target"] = targets[0].get("id")
    source["inspect"] = {"candidate": cid}
    if source.get("target"):
        source["inspect"]["target"] = source["target"]
    frontier = connections(targets)
    if frontier:
        source["frontier"] = frontier
    view["decision"] = describe(plan, state, cid)


def connections(targets):
    """Expose different connected scopes before spending both slots on sibling variants."""
    groups = []
    for relations in ({"writer", "reader", "transfer-or-allocation"}, {"existing-variant"}, {"callee"}):
        groups.append([t for t in targets if (t.get("dependency") or {}).get("relation") in relations])
    chosen = [group[0] for group in groups if group][:2]
    for target in (item for group in groups for item in group):
        if len(chosen) < 2 and target not in chosen:
            chosen.append(target)
    return [{"id": t.get("id"), "relation": t["dependency"]["relation"],
             "symbol": t.get("symbol") or t.get("hotspot")} for t in chosen]
