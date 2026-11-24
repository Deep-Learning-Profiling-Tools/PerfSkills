"""Compare edit-now with one observation followed by an edit, under the same horizon.

All gains are nonnegative log improvements of the declared objective. The score
is a scheduling heuristic. Cold cost/utility priors are explicit and are replaced
by comparable observations. Seconds and tokens have separate opportunity prices.
"""

import math
from statistics import median

from decision_learning import incumbent_gain


def edit_context(plan, state, current_s):
    screens = [b for b in state.get("batches", []) if b.get("phase") == "screen"]
    durations = [(b.get("cost") or {}).get("campaign_command_s") for b in screens
                 if not (b.get("cost") or {}).get("campaign_cached")]
    seconds = [x for x in (durations + [current_s])[-5:] if x and x > 0]
    rows = [(state["candidates"].get(cid) or {}).get("result") or {}
            for b in state.get("batches", []) for cid in b.get("candidates", [])]
    gains = [gain for row in rows[-5:] if (gain := incumbent_gain(row)) is not None]
    prior = math.log(max(1.001, plan.get("sampling", {}).get("practical_threshold", 1.02)))
    from diagnostic_context import refined, gains as progress
    learned = progress(plan, state, rows[-5:]) if refined(plan) else {}
    return {"edit_s": median(seconds) if seconds else None, "minimum_gain": prior,
            "edit_gain": (prior + sum(gains)) / (1 + len(gains)),
            "stable_screens": len(gains), **learned}


def forecast(option, record, experience, wall):
    recent = record.get("success_costs") or []
    # Transfer only the observed overhead beyond process wall; local observations
    # already include launch, capture, replay, export and reduction.
    local = option.get("measured_s")
    predicted = [max(0, s - app) + wall for s, app in recent if app is not None and wall is not None]
    if local is not None:
        cost, basis = local, "run-question-wall"
    elif predicted:
        cost, basis = median(predicted), "environment-adjusted-wall"
    else:
        rank = option["cost_rank"]
        # Cold startup prior (seconds) and extra application work. Never a measurement.
        cost, basis = (wall or 1.) * (1 + (rank - 1) / 4) + .25 * rank ** 2, "cold-prior"
    margin = median(abs(v - cost) for v in predicted) if len(predicted) > 1 else cost * .25
    deliveries = experience.get("deliveries") or []
    return {"wall_s": cost, "reserve_s": cost + margin, "cost_basis": basis,
            "packet_tokens": median([d["tokens"] for d in deliveries]) if deliveries else 320,
            "token_basis": "observed-packet" if deliveries else "cold-prior"}


def score(context, cost, record, experience, opportunity, novelty, token_limit=640):
    attempts = record.get("attempts", 0)
    coverage = (1 + record.get("coverage_sum", 0)) / (1 + attempts)
    deliveries = experience.get("deliveries") or []
    visibility = (1 + sum(d["visible"] for d in deliveries)) / (1 + len(deliveries))
    # A cold question is worth testing if it could expose a practical actionable
    # lever. A historical Amdahl fraction weights this prior only.
    prior = context["minimum_gain"] * (1 + (opportunity if opportunity is not None else .5))
    outcomes = list((experience.get("outcomes") or {}).values())
    guided = (prior + sum(gain * weight for gain, weight in outcomes)) / (1 + sum(w for _, w in outcomes))
    guided *= coverage * visibility * novelty
    # Guided and unguided actions both pay for the next edit. The extra capture
    # consumes a fraction of another edit, plus its evidence packet allowance.
    extra_wall = cost["wall_s"] / context["edit_s"]
    token_charge = .25 * context["minimum_gain"] * cost["packet_tokens"] / token_limit
    return guided - context["edit_gain"] * (1 + extra_wall) - token_charge
