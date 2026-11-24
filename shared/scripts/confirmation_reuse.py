"""Reuse successful independent timings only under their unchanged fingerprint."""

from campaign_io import campaign_fingerprint, REPO
from experiment_support import load


def prior_final(run, state, ids):
    entry = next((b for b in reversed(state.get("batches", []))
                  if b.get("phase") == "confirm" and set(b.get("candidates", [])) == set(ids)), None)
    if not entry:
        raise ValueError("no matching independent final campaign to resume")
    batch = run / "batches" / entry["id"]
    plan, result = load(batch / "campaign-plan.json", {}), load(batch / "campaign-result.json", {})
    gates = result.get("final", {}).get("gates") or {}
    if result.get("status") != "complete" or not gates or not all(gates.values()):
        raise ValueError("independent timing gates did not pass; evidence-only retry cannot repair statistics")
    variables = {"repo": str(REPO), "run_id": plan["run_id"], "run_dir": str(batch)}
    current = campaign_fingerprint(plan, variables)
    if current != result.get("fingerprint") or current != result.get("fingerprint_after"):
        raise ValueError("final campaign inputs changed; independent timing cannot be reused")
    return entry["id"], batch, plan, result
