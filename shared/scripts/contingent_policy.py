"""Finite-horizon value of a diagnostic that can change which rewrite is tried.

A useful measurement raises the expected value of its linked rewrite. An empty
one leaves the competing edit available. We may consume that rewrite once, then
spend remaining slots on ordinary edits. Predictions schedule work only.
"""

import math
import time
from functools import lru_cache
from acquisition_value import edit_context
from rewrite_evidence import enabled
from skill_binding import digest
from decision_learning import objective


def branch(edit_gain, magnitude, probability, slots):
    """After a successful hypothesis test, a dependent refinement becomes feasible.

    On a miss the tested hypothesis retires. A generic edit spends one slot
    and leaves the untested hypothesis available. Only three slots are expanded.
    """
    @lru_cache(None)
    def solve(left, stage):
        if not left:
            return 0., "finish"
        if stage == 2:
            return left * edit_gain, "independent-edit"
        chance = probability if stage == 0 else (1 + probability) / 2
        gain = magnitude if stage == 0 else magnitude / 2
        ordinary = edit_gain + solve(left-1, stage)[0]
        informed = chance * (gain + solve(left-1, stage+1)[0]) + (1-chance) * solve(left-1, 2)[0]
        if informed >= ordinary - 1e-12:
            return informed, "linked-rewrite" if stage == 0 else "dependent-refinement"
        return ordinary, "independent-edit"
    return solve(slots, 0)


def value(plan, costs, cost, record, history, leverage, novelty, slots, slack_s, price,
          hypothesis, memory, provider, nature, reuse):
    gain = costs["edit_gain"]
    slots = min(3, slots)
    if slack_s is not None:
        slots = min(slots, max(0, int(slack_s / costs["edit_s"])))
    p = (1 + record.get("coverage_sum", 0)) / (2 + record.get("attempts", 0))
    visibility = history.get("deliveries") or []
    p *= (1 + sum(d["visible"] for d in visibility)) / (1 + len(visibility))
    p = max(0., min(1., p * novelty))
    samples = list(memory.get("hypothesis-" + digest([objective(plan), nature, provider, hypothesis]), {}).values())
    prior = costs["minimum_gain"] * (2 + (leverage if leverage is not None else .5))
    loose = list((history.get("outcomes") or {}).values())
    base = (prior + sum(x*w for x,w in loose)) / (1 + sum(w for _,w in loose))
    from diagnostic_context import refined
    if refined(plan):
        # Preserve the observed spread of rewrite gains. A prior structural win
        # is an uncertain opportunity size, not an indefinitely recurring edit reward.
        tail = costs.get("win_magnitude", costs["minimum_gain"] * 3)
        strength = leverage if leverage is not None else .5
        prior = max(prior, 2 * math.sqrt(tail * costs["minimum_gain"]) * (.5 + strength/2))
        base = (prior + sum(x*w for x,w in loose)) / (1 + sum(w for _,w in loose))
    linked_gain = (base + sum(samples)) / (1 + len(samples))
    remaining = slots if slack_s is None else min(slots, max(0, int((slack_s-cost["reserve_s"]) / costs["edit_s"])))
    support = (1 + sum(x > 0 for x in samples)) / (2 + len(samples))
    # Diagnostic support/contradiction updates a latent binary mechanism. These
    # reliability priors are explicit scheduling assumptions, not measured confidence.
    reliable = .8
    positive = support*reliable + (1-support)*(1-reliable)
    on_support = support*reliable / positive
    on_contradiction = support*(1-reliable) / (1-positive)
    if reuse:
        p *= .5  # A second view of the same mapped producer has less marginal novelty.
    actionable = reuse or not hypothesis.endswith("-guided-rewrite") or bool(samples)
    # A capability label alone does not identify an editable site or concrete rewrite.
    # Existing cited context or an agent-specified proposal makes the direct branch feasible.
    immediate, first = branch(gain, linked_gain, support if actionable else 0., slots)
    empty, unchanged = branch(gain, linked_gain, support if actionable else 0., remaining)
    yes, then = branch(gain, linked_gain, on_support, remaining)
    no, other = branch(gain, linked_gain, on_contradiction, remaining)
    charge = gain * cost["wall_s"] / costs["edit_s"] + costs["minimum_gain"] * (.25 * cost["packet_tokens"]/640 + price)
    utility = p * (positive * yes + (1-positive) * no) + (1-p) * empty - immediate - charge
    return utility, {"horizon": slots, "slots_after_capture": remaining, "hypothesis": hypothesis,
                     "concrete_before_capture": bool(actionable),
                     "coverage_probability": p, "support_prior": support, "reliability_prior": reliable,
                     "edit_without_capture": first, "if_supported": then, "if_contradicted": other, "if_empty": unchanged,
                     "rewrite_samples": len(samples), "edit_now_value": immediate, "capture_charge": charge,
                     "hypothesis_gain_prior": prior, "gain_basis": "observed-tail-with-practical-prior" if costs.get("positive_screens") else "cold-prior"}


def stop_reason(plan, run, state, current_s=None, choice=None):
    """Only a flexible contract may close before the declared upper bound."""
    if not enabled(plan) or not plan.get("budget", {}).get("flexible"):
        return None
    if state.get("early_stop"):
        return state["early_stop"]
    measured = [m for m in state.get("candidates", {}).values() if m.get("result")]
    if not measured or not any((m["result"].get("correctness") or {}).get("passed") for m in measured):
        return None
    costs = edit_context(plan, state, current_s)
    settings = plan["profiling"]["skill_runtime"]
    deadline = settings.get("campaign_deadline")
    if deadline and costs["edit_s"] and deadline - time.time() < costs["edit_s"]:
        return "remaining-wall-budget-below-observed-edit-cost"
    from resource_meter import totals
    limits = (settings.get("resources") or {}).get("budgets") or {}
    spent = totals(run) if limits else {}
    if any(spent.get(k) is not None and spent[k] >= cap for k, cap in limits.items()):
        return "declared-resource-budget-spent"
    # Conserve budget only after stable screens, a demonstrated gain, a priced
    # diagnostic menu with no profitable capture, and no agent-declared open alternative.
    floor = settings.get("minimum_expected_log_gain")
    if floor and choice and len(measured) >= 2 and costs["stable_screens"] >= 2 and (
            (state.get("best") or {}).get("improvement", 0) > 1 and costs["edit_gain"] < floor and
            choice.get("tool") == "none" and not (measured[-1].get("hypothesis") or {}).get("alternatives")):
        return "expected-edit-gain-below-declared-floor"
    return None
