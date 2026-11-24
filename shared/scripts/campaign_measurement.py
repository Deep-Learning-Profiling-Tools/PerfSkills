"""Build and measure candidates, then checkpoint results before diagnostics."""

from pathlib import Path
import json
import subprocess
import sys
import time
from adaptive_policy import enabled
from acquisition_limits import limited
from acquisition_cost import event as cost_event
from candidate_workspace import campaign_plan, compact_candidate
from campaign_stats import summarize, improvement, SEED
from experiment_state import save_state
from experiment_support import REPO, load, write


def measure_candidates(plan: dict, plan_path: Path, run: Path, state: dict,
                  baseline: Path, ids: list[str], phase: str,
                  diagnose_ids=None, evidence="auto", needs=None, provider=None, retry_evidence=False) -> dict:
    adaptive_final = enabled(plan) and phase == "confirm" and 1 <= len(ids) <= 2
    if len(ids) != 1 and not adaptive_final:
        raise ValueError("evaluate/confirm requires exactly one candidate id")
    incumbent_before = (state.get("best") or {}).get("improvement", 1.0)
    if enabled(plan) and state.get("best_kernel") and state["best_kernel"].get("duration_ns") is None:
        state.pop("best_kernel", None)
    force_ncu = set(diagnose_ids or [])
    unknown_diagnose = force_ncu - set(ids)
    if unknown_diagnose:
        raise ValueError(f"--ncu candidates are not in this batch: {','.join(sorted(unknown_diagnose))}")
    if force_ncu and not (plan.get("kernel_profile") or {}).get("enabled"):
        raise ValueError("--ncu requires kernel_profile.enabled=true in the experiment plan")
    sequence = len(state["batches"]) + 1
    name = (f"screen-{sequence:02d}" if phase == "screen" else f"final-{sequence:02d}")
    batch = run / "batches" / name
    batch.mkdir(parents=True, exist_ok=True)
    if retry_evidence:
        if phase != "confirm" or not enabled(plan):
            raise ValueError("evidence-only retry requires adaptive final confirmation")
        from confirmation_reuse import prior_final
        name, batch, generated, reused = prior_final(run, state, ids)
    else:
        generated = campaign_plan(plan, run, baseline, state, ids, phase, batch)
    generated_path = batch / "campaign-plan.json"
    if not retry_evidence:
        write(generated_path, generated)
    raw = run / "raw/experiment"
    raw.mkdir(parents=True, exist_ok=True)
    campaign_started = time.perf_counter()
    from shared_activity_measurement import enabled as shared_activity, run as run_shared
    if shared_activity(plan,state,phase) and not retry_evidence:
        proc = run_shared(plan,run,state,baseline,generated,batch)
    elif retry_evidence:
        from types import SimpleNamespace
        proc = SimpleNamespace(returncode=0, stdout='{"status":"cached"}', stderr="")
    else:
        proc = subprocess.run([sys.executable, str(REPO / "shared/scripts/campaign.py"), str(generated_path)],
                              cwd=REPO, capture_output=True, text=True)
    (raw / f"{name}.stdout.txt").write_text(proc.stdout)
    (raw / f"{name}.stderr.txt").write_text(proc.stderr)
    result = reused if retry_evidence else load(batch / "campaign-result.json", {})
    try:
        campaign_cached = json.loads(proc.stdout).get("status") == "cached"
    except ValueError:
        campaign_cached = False
    campaign_spent = time.perf_counter() - campaign_started
    cost_event(run, "campaign-command", campaign_spent, batch=name, phase=phase,
               cache_hit=campaign_cached, returncode=proc.returncode,
               components=result.get("cost") if not campaign_cached else None)
    if proc.returncode or result.get("status") != "complete":
        error = (raw / f"{name}.stderr.txt").read_text().strip()
        if not error:
            error = proc.stdout.strip()[-1500:]
        raise RuntimeError(f"{phase} campaign failed; {error[:1800]}; see {raw / f'{name}.stderr.txt'}")
    rows = [compact_candidate(row) for row in result["candidates"]]
    if enabled(plan) and phase == "confirm":
        final = result["final"]
        samples = final["samples"]
        base_rows = samples["baseline"]
        for row in rows:
            current = samples[row["id"]]
            if row.get("ranking"):
                row["screening_ranking"] = row.pop("ranking")
            row["confidence"] = "independent-final-pairs"
            row.update({"pairs": final["pairs"], "stop": final["stop"],
                        "metric": summarize([x["metric"] for x in current]),
                        "baseline_metric": summarize([x["metric"] for x in base_rows]),
                        "improvement": improvement([x["metric"] for x in base_rows],
                            [x["metric"] for x in current], plan["metric"].get("direction", "lower"),
                            plan["sampling"].get("bootstrap_iterations", 1000), SEED + 700),
                        "wall_improvement": improvement([x["wall_s"] for x in base_rows],
                            [x["wall_s"] for x in current], "lower",
                            plan["sampling"].get("bootstrap_iterations", 1000), SEED + 701),
                        "correctness": {"passed": final["gates"]["correctness"]}})
    accepted = phase != "confirm" or all(result["final"].get("gates", {}).values())
    by_id = {row["id"]: row for row in rows}
    for candidate_id, row in by_id.items():
        from decision_learning import active
        if active(plan) and phase == "screen":
            meta = state["candidates"][candidate_id]
            if meta.get("measurement_fingerprint") != result.get("fingerprint_after"):
                meta.pop("profile", None)
            meta["measurement_fingerprint"] = result.get("fingerprint_after")
        state["candidates"][candidate_id]["result"] = row
        from change_manifest import build as record_changes
        record_changes(plan, run, state, candidate_id)
        state["candidates"][candidate_id]["status"] = (
            "screened" if enabled(plan) or phase == "screen" or not accepted or
            candidate_id != result["final"]["winner"] else "confirmed")
    from shared_activity_measurement import publish_capture
    from cupti_evidence import enabled as event_timing, retain
    if event_timing(plan):
        retain(plan, load(batch / "primary-activity.json", {}))
    publish_capture(plan,run,state,batch,ids,result.get("fingerprint_after"))
    save_state(run, state)
    if limited(plan) and phase == "screen":
        from measured_checkpoint import pending
        pending(plan, run, state, batch, ids, incumbent_before, by_id)
    return {"batch": batch, "name": name, "result": result, "by_id": by_id, "accepted": accepted,
            "campaign_spent": campaign_spent, "campaign_cached": campaign_cached,
            "generated_path": generated_path, "incumbent_before": incumbent_before, "force_ncu": force_ncu}
