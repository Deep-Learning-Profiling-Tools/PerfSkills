"""Choose among feasible evidence questions, providers and continuing with existing facts."""

import time

from capture_reuse import remaining_time
from capture_history import known_capabilities
from acquisition_value import edit_context, forecast, score
from diagnostic_context import refined, misses, opportunity
from decision_learning import memory_path, objective
from rewrite_evidence import enabled as lookahead
from contingent_policy import stop_reason, value as contingent_value
from resource_meter import ledger, price, totals
from evidence_opportunity import assess
from experiment_support import load, write
from skill_binding import binding, context, digest, recipe_key, store_path


def plateau_questions(plan, nature, acquisition):
    if "gpu-idle-visible" in nature:
        options = [["sync", "launch-overhead"], ["call-context"], ["transfer"]]
    else:
        options = [["instruction-stalls"], ["memory-bandwidth", "gpu-occupancy"],
                   ["causal-stall-attribution"]]
        if refined(plan):
            options += [["gpu-pc", "instruction-stalls"], ["gpu-source-line", "instruction-stalls"]]
    options += acquisition.get("question_alternatives") or []
    if "gvprof" in ((plan.get("profiling") or {}).get("artifacts") or []):
        options.append(["value-redundancy"])
    return [list(q) for q in dict.fromkeys(tuple(sorted(q)) for q in options if q)]


def repeated_branch_loss(state, row):
    """Two linked correct edits both missed the incumbent even at their observed upper bounds."""
    parent = (state.get("candidates", {}).get(row["id"]) or {}).get("parent")
    prior = (state.get("candidates", {}).get(parent) or {}).get("result") or {}
    for sample in (row, prior):
        ranking = sample.get("ranking") or {}
        envelope = ranking.get("speedup_envelope")
        if (not (sample.get("correctness") or {}).get("passed") or not envelope or
                envelope[1] >= max(1., ranking.get("incumbent_at_screen") or 1.)):
            return False
    return True


def choose(plan, run, state, row, decision, campaign_s, router):
    reason = decision.get("reason")
    cid = row["id"]
    # Discovery, explicit requests, changed identities and collateral gates retain
    # their own capability contracts. Only discretionary evidence competes with editing.
    plateau = reason in {"plateau-needs-mechanism-evidence", "recent-mechanism-evidence-still-valid"}
    if reason == "uncertain-timing-does-not-justify-counters" and repeated_branch_loss(state, row):
        plateau = True
    if (refined(plan) and reason == "uncertain-timing-does-not-justify-counters" and
            misses(state, row, plan.get("sampling", {}).get("practical_threshold", 1.02)) >= 2):
        plateau = True
    optional = plateau or reason in {"static-resource-growth", "large-gain-remap-hotspots",
                                    "aged-evidence-with-runtime-movement"}
    advanced = lookahead(plan)
    declared = ((state.get("candidates", {}).get(cid) or {}).get("hypothesis") or {}).get("alternatives") or {}
    if advanced and declared and reason in {"recent-mechanism-evidence-still-valid", "reuse-current-evidence",
                                            "timing-and-static-evidence-sufficient"}:
        optional, plateau = True, True
    early = stop_reason(plan, run, state, campaign_s)
    if early:
        state["early_stop"] = early
        return {"tool": "none", "reason": early, "action": "finish", "screening_only": True}
    from capture_reuse import before_optional, record as record_reuse
    reuse = before_optional(plan, state, row, decision)
    if reuse:
        record_reuse(plan, run, state, row["id"], decision.get("reason"), reuse)
        return reuse
    if not optional:
        if reason == "uncertain-timing-does-not-justify-counters":
            decision = decision | {"action": "compare-source-hypotheses"}
        return router(plan, state, decision, None)
    nature = context(plan, state)
    target = ((state.get("best") or {}).get("id") or cid) if plateau else cid
    if plateau and (state.get("best") or {}).get("improvement", 1) <= 1:
        target = "baseline"
    if advanced and declared:
        target = cid
    seen = known_capabilities(run, state, target)
    phase = {}
    # A plateau on a new branch may warrant a complementary view even with recent counters.
    if plateau:
        from phase_opportunity import current
        phase = current(run, plan, state, target)
        questions = plateau_questions(plan, nature, decision)
        if phase.get("kind") == "unattributed-gap":
            questions = [phase["needs"], *(decision.get("question_alternatives") or [])]
    elif reason == "static-resource-growth":
        questions = [["register-pressure", "gpu-occupancy"]]
    else:
        from evidence_scope import requires_roi
        questions = [["gpu-hotspot"] + (["roi-attribution"] if requires_roi(plan) else [])]
    if advanced and declared:
        questions = list(dict.fromkeys(tuple(q) for q in ([[q] for q in declared] + questions)))
        questions = [list(q) for q in questions]
    bound = binding(plan)
    local_binding = (state.get("skill_runtime") or {}).get("binding")
    routing_state = state if local_binding in {None, bound["key"]} else state | {"acquisition_costs": {}}
    recipes = load(store_path(plan, bound), {}).get("recipes", {}) if bound["observed"] else {}
    experience = load(memory_path(plan, bound["key"]), {}) if bound["observed"] else {}
    prior, learned, usage, spent = None, {}, [], {}
    if advanced:
        from evidence_dependencies import historical
        prior = historical(run, state, target, bound["key"])
        learned = load(memory_path(plan, bound["key"]).with_suffix(".rewrites.json"), {}) if bound["observed"] else {}
        usage = ledger(run)
        spent = totals(run, usage)
    wall = (load(run / "baseline-timing.json", {}).get("sample") or {}).get("wall_s")
    costs = edit_context(plan, state, campaign_s)
    leverage = assess(plan, state, row, decision).get("recoverable_roi_fraction_bound")
    if leverage is None and refined(plan):
        leverage = opportunity(plan, run, state, target)
    settings = plan["profiling"]["skill_runtime"]
    deadline = settings.get("campaign_deadline")
    slots = max(1, plan.get("budget", {}).get("iterations", 1) -
                sum(bool(m.get("result")) for m in state.get("candidates", {}).values()))
    edit_reserve = (min(slots, 1) if plan.get("budget", {}).get("flexible") else slots) * (costs["edit_s"] or 0)
    base = {k: v for k, v in decision.items() if k != "question_alternatives"}
    base.update(tool="kernel" if plateau else decision["tool"], question_origin="policy")
    if plateau:
        base["reason"] = "plateau-needs-mechanism-evidence"
    options = []
    selected, best = None, 0.
    for needs in questions:
        if remaining_time() == 0:
            options.append({"disposition": "optional-acquisition-budget-exhausted"})
            break
        missing = set(needs) - seen
        if not missing:
            options.append({"needs": needs, "disposition": "already-observed"})
            continue
        menu = router(plan, routing_state, base, needs, menu=True)
        if not menu.get("candidates"):
            options.append({"needs": needs, "disposition": "unavailable", "unavailable": menu.get("unavailable")})
        for candidate in menu.get("candidates", []):
            key = recipe_key(candidate, needs, nature)
            record = recipes.get(key) or {}
            history = experience.get(digest([key, objective(plan)])) or {}
            cost = forecast(candidate, record, history, wall)
            item = {"provider": candidate["skill"], "needs": needs, **cost}
            options.append(item)
            if record.get("consecutive_failures", 0) >= 2:
                item["disposition"] = "rolled-back"
                continue
            time_left = remaining_time()
            if deadline is not None:
                time_left = min(time_left if time_left is not None else float("inf"), deadline - time.time() - edit_reserve)
            if (time_left is not None and cost["reserve_s"] > time_left) or not costs["edit_s"]:
                item["disposition"] = "insufficient-time-or-cost-evidence"
                continue
            if advanced:
                estimate, penalty = price(plan, usage, spent, candidate["skill"], cost["wall_s"], cost["packet_tokens"])
                item["resource_forecast"] = estimate
                if penalty is None:
                    item["disposition"] = "insufficient-resource-budget-or-meter"
                    continue
                hypothesis = next((declared[n] for n in needs if n in declared), needs[0] + "-guided-rewrite")
                value, item["contingency"] = contingent_value(plan, costs, cost, record, history, leverage,
                    len(missing) / len(needs), slots, deadline-time.time() if deadline else None, penalty,
                    hypothesis, learned, candidate["skill"], nature, prior and prior["provider"] == candidate["skill"])
            else:
                value = score(costs, cost, record, history, leverage, len(missing) / len(needs))
            item["net_value"] = value
            if value > best:
                best = value
                selected = base | {"tool": candidate["tool"], "provider": candidate["skill"],
                    "needs": needs, "cost_key": candidate["cost_key"], "measured_cost_s": candidate["measured_s"],
                    "cost_basis": cost["cost_basis"], "action": "profile-then-edit", "policy_target": target}
    exhausted = remaining_time() == 0
    if exhausted:
        selected = None
    action = selected or {"tool": "none", "reason": "optional-acquisition-budget-exhausted" if exhausted else "use-existing-evidence",
                          "action": "compare-source-hypotheses" if plateau else "edit",
                          "screening_only": True}
    if prior and not selected:
        action["historical_fact"] = prior
    early = stop_reason(plan, run, state, campaign_s, action) if options else None
    if early:
        state["early_stop"] = early
        action.update(action="finish", reason=early)
    write(run / "candidates" / cid / "acquisition-decision.json",
          {"policy": settings["decision_policy"], "trigger": reason, "snapshot": target, "edit": costs,
           "opportunity_bound": leverage, "campaign_edit_reserve_s": edit_reserve if deadline else None,
           "options": options, "selected": action, "historical_prior": prior,
           "phase_opportunity": phase or None,
           "score_role": "scheduling-heuristic-in-log-gain-units"})
    return action
