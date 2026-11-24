"""Publish campaign results and reuse the configured finalization policy."""

import shutil
import time
from adaptive_policy import enabled
from acquisition_cost import event as cost_event
from experiment_state import save_state
from experiment_support import load, write
from screening_quality import confirmation_shortlist
from terminal_view import terminal_ncu
from selection_objective import notice, kernel_mode
from screen_outcome import terminal as screen_outcome


def publish(plan, plan_path, run, state, baseline, ids, phase, session, profiles, reference_profiles,
            retry_evidence, *, run_campaign, run_final_stages, refresh_brief, terminal_candidate):
    batch, name, result, by_id, accepted = (session[k] for k in ("batch", "name", "result", "by_id", "accepted"))
    campaign_spent, campaign_cached, generated_path, incumbent_before = (
        session[k] for k in ("campaign_spent", "campaign_cached", "generated_path", "incumbent_before"))
    baseline_kernel = (state.get("kernel_profiles") or {}).get("baseline")
    winner = result["final"]["winner"]
    point = result["final"]["baseline_vs_winner"].get("point")
    winner_profile = next((item for item in profiles if item["id"] == winner), None)
    timed_profiles = [item for item in profiles
                      if item.get("status") == "complete" and
                      isinstance(((item.get("ncu") or {}).get("duration_ns")), (int, float))]
    kernel_winner = min(timed_profiles, key=lambda item: item["ncu"]["duration_ns"],
                        default=winner_profile)
    if kernel_winner and kernel_winner.get("status") == "complete":
        kernel_ncu = kernel_winner.get("ncu") or {}
        baseline_duration = (baseline_kernel or {}).get("duration_ns")
        duration = kernel_ncu.get("duration_ns")
        kernel_speedup = (baseline_duration / duration if baseline_duration and duration else None)
        prior = state.get("best_kernel") or {}
        prior_duration = prior.get("duration_ns")
        tied_but_better_roi = (duration is not None and duration == prior_duration and
                               point is not None and point > (prior.get("roi_improvement") or 0))
        if duration is not None and (prior_duration is None or
                (duration is not None and duration < prior_duration) or tied_but_better_roi):
            state["profile_frontier"] = {
                "candidate": kernel_winner["id"], "target": kernel_winner.get("target"),
                "next": kernel_winner.get("next"), "ncu_advice": kernel_ncu.get("advice"),
                "kernel_duration_ns": duration, "kernel_speedup": kernel_speedup,
                "artifact": kernel_winner.get("artifact")}
            state["best_kernel"] = {"id": kernel_winner["id"], "duration_ns": duration,
                                    "speedup": kernel_speedup, "roi_improvement": point}
    state["batches"] = [b for b in state["batches"] if b["id"] != name]
    state["batches"].append({"id": name, "phase": phase, "candidates": ids,
                             "incumbent_before": incumbent_before,
                             "winner": winner, "improvement": point,
                             "profiles": [{"id": item["id"], "status": item["status"],
                                           "artifact": item.get("artifact")} for item in profiles + reference_profiles],
                             "artifact": str(batch / "campaign-result.json"),
                             "cost": {"campaign_s": result.get("elapsed_s"),
                                      "campaign_command_s": campaign_spent, "campaign_cached": campaign_cached,
                                      "components": result.get("cost"),
                                      "profile_s": sum(x.get("spent_s", 0) for x in profiles),
                                      "static_s": sum((x.get("static") or {}).get("spent_s", 0) for x in profiles)}})
    better = not kernel_mode(state) and point is not None and point > (state.get("best") or {}).get("improvement", 0)
    if not kernel_mode(state) and enabled(plan) and state.get("best") and phase == "screen":
        old = (state["candidates"].get(state["best"]["id"], {}).get("result") or {}).get("metric") or {}
        new = by_id[winner].get("metric") or {}
        if old.get("median") is not None and new.get("median") is not None:
            better = new["median"] < old["median"] if plan["metric"].get("direction", "lower") == "lower" else new["median"] > old["median"]
    if not kernel_mode(state) and (phase == "confirm" or not state.get("best") or better):
        state["best"] = {"id": winner, "improvement": point,
                         "workspace": state["candidates"][winner]["workspace"]}
    from selection_objective import reconcile
    reconcile(plan, run, state)
    if phase == "confirm":
        shutil.copy2(generated_path, run / "campaign-plan.json")
        shutil.copy2(batch / "campaign-result.json", run / "campaign-result.json")
        shutil.copy2(batch / "campaign-brief.txt", run / "campaign-brief.txt")
        state["last_confirmation"] = {"winner": winner, "accepted": accepted,
                                      "gates": result["final"].get("gates", {})}
        if enabled(plan) and not accepted:
            write(run / "final-verdict.json", {
                "protocol": "profileweaver.final-verdict/v1", "status": "inconclusive",
                "winner": winner, "improvement": result["final"]["baseline_vs_winner"],
                "pairs": result["final"]["pairs"], "gates": result["final"]["gates"],
                "note": "Independent statistical gates did not pass; final profiling was not spent."})
        if accepted:
            if not enabled(plan):
                state["confirmed"] = {"id": winner, "improvement": point,
                                      "workspace": state["candidates"][winner]["workspace"]}
            # The finalizer reads experiment-state.json. Persist confirmation before invoking it.
            save_state(run, state)
            final_started = time.perf_counter()
            try:
                run_final_stages(plan, run, state, result)
            finally:
                cost_event(run, "final-evidence", time.perf_counter() - final_started, candidate=winner)
            if enabled(plan):
                report = load(run / "report.json", {})
                kernel = (state.get("kernel_profiles") or {}).get("final") or {}
                accepted = report.get("status") == "verified" and bool(kernel.get("duration_ns"))
                state["last_confirmation"]["accepted"] = accepted
                state["last_confirmation"]["profile_gates"] = report.get("gates")
                if accepted:
                    state["candidates"][winner]["status"] = "confirmed"
                    state["confirmed"] = {"id": winner, "improvement": point,
                        "workspace": state["candidates"][winner]["workspace"], "compared_candidates": ids}
                    state["best_kernel"] = {"id": winner, "duration_ns": kernel["duration_ns"],
                                            "speedup": None, "roi_improvement": point}
                write(run / "final-verdict.json", {
                    "protocol": "profileweaver.final-verdict/v1",
                    "status": "verified" if accepted else "inconclusive",
                    "winner": winner, "improvement": result["final"]["baseline_vs_winner"],
                    "pairs": result["final"]["pairs"], "gates": report.get("gates"),
                    "wall_improvement": result["final"]["wall_baseline_vs_winner"],
                    "wall_policy": plan["sampling"].get("wall_gate", "advisory"),
                    "kernel_evidence": bool(kernel.get("duration_ns")),
                    "selection": result["final"].get("selection"),
                    "evidence": {"mode": "independent-confirmation-and-winner-profiles",
                                 "report": str(run / "report.json")}})
    save_state(run, state)
    refresh_brief(plan, run, state)
    if enabled(plan):
        from cupti_evidence import build as build_iteration_evidence
        for capsule in profiles:
            capsule["agent_packet"] = build_iteration_evidence(
                plan, run, state, capsule, by_id[capsule["id"]], confirmation=phase == "confirm")
            from decision_learning import active, forwarded
            if active(plan):
                forwarded(plan, run, state, reference_profiles, capsule["agent_packet"])
    automatic = None
    measured = sum(1 for item in state["candidates"].values() if item.get("result"))
    budget = (plan.get("budget") or {}).get("iterations")
    if phase == "screen" and budget and (measured >= budget or state.get("early_stop")) and not state.get("confirmed"):
        if plan["sampling"].get("measurement_policy") == "single-or-two-v3":
            from measured_finalization import finalize
            result = finalize(plan, run, state, baseline)
            return result | ({"stop_reason": state["early_stop"]} if state.get("early_stop") else {})
        if (plan.get("sampling") or {}).get("mode") in {"winner-confirmation", "adaptive"}:
            winner_id = (state.get("best") or {}).get("id")
            if not winner_id:
                raise RuntimeError("winner-confirmation mode has no measured winner")
            save_state(run, state)
            finalists = (confirmation_shortlist(state, min(2, plan["sampling"].get("finalists", 2)), plan["metric"].get("direction", "lower"))
                         if enabled(plan) else [winner_id])
            automatic = {"mode": "bounded-finalist-confirmation" if len(finalists) > 1 else "winner-only-confirmation",
                         "result": run_campaign(plan, plan_path, run, state, baseline,
                                                finalists, "confirm")}
        else:
            from experiment_finalize import promote_existing
            automatic = promote_existing(plan, run, state)
    if automatic and plan["sampling"].get("confirmation_policy") == "bounded-v2":
        completed = automatic["result"]
        return completed | {"campaign_complete": True, "next_action": "report-result",
                            "last_screen": {"candidate": ids[0], "artifact": str(batch / "campaign-result.json")}}
    return {"status": "complete" if accepted else "inconclusive", "phase": phase,
            **({"campaign_complete": True, "next_action": "report-result"} if phase == "confirm" else {}),
            **({"confidence": "screening-only", "verified": False}
               if enabled(plan) and phase == "screen" else {}),
            "accepted": accepted, "winner": winner, "improvement": point,
            **({"kernel_winner": state.get("best_kernel")}
               if plan["sampling"].get("measurement_policy") != "single-or-two-v3" else {}),
            "pairs": result["final"]["pairs"],
            **({"benchmark_pairs_reused": result["final"]["pairs"], "new_benchmark_runs": 0} if retry_evidence else {}),
            **({"selection": result["final"]["selection"]} if result["final"].get("selection") else {}),
            "profiles": [terminal_candidate(item, by_id[item["id"]],
                                             confirmation=phase == "confirm")
                         for item in profiles], "batch": name,
            **({"automatic_finalization": automatic} if automatic else {}), **notice(state),
            **(screen_outcome(plan, state, ids[0], accepted) if phase == "screen" else {}),
            **({"final_ncu": terminal_ncu((state.get("kernel_profiles") or {}).get("final"))}
               if not enabled(plan) and accepted and (state.get("kernel_profiles") or {}).get("final") else {})}

