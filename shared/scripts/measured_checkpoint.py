"""Publish completed measurements before optional diagnostics can fail or be cancelled."""

from experiment_state import save_state


def select_best(plan, state, candidate, row):
    from selection_objective import kernel_mode
    if kernel_mode(state):
        return
    if not (row.get("correctness") or {}).get("passed"):
        return
    value = (row.get("metric") or {}).get("median")
    old_id = (state.get("best") or {}).get("id")
    old = (((state.get("candidates") or {}).get(old_id) or {}).get("result") or {}).get("metric") or {}
    previous = old.get("median")
    better = value is not None and (previous is None or
        (value < previous if plan["metric"].get("direction", "lower") == "lower" else value > previous))
    if better:
        state["best"] = {"id": candidate, "improvement": (row.get("improvement") or {}).get("point"),
                         "workspace": state["candidates"][candidate]["workspace"]}


def pending(plan, run, state, batch, ids, incumbent, rows):
    for candidate in ids:
        select_best(plan, state, candidate, rows[candidate])
    state["batches"].append({"id": batch.name, "phase": "screen-pending-evidence", "candidates": ids,
         "incumbent_before": incumbent, "profiles": [], "artifact": str(batch / "campaign-result.json")})
    save_state(run, state)


def recover(plan, run, state, candidate, entry):
    row = state["candidates"][candidate]["result"]
    select_best(plan, state, candidate, row)
    known = next((b for b in state.get("batches", []) if b["id"] == entry["id"]), None)
    if known is None:
        known = dict(entry);state.setdefault("batches", []).append(known)
    if known.get("phase") in {None, "screen-pending-evidence"}:
        known.update({"phase": "screen", "profiles": [], "evidence_status": "not-completed"})
    save_state(run, state)
