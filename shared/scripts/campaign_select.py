#!/usr/bin/env python3
"""Select screened candidates and confirm finalists with explicit gates."""

from __future__ import annotations

from campaign_run import run_command
from campaign_stats import SEED, improvement
from statistics import median
from screening_quality import precision_envelope


def final_confirmation(base, candidates, initial, plan, run_dir, variables):
    sampling = plan["sampling"]
    eligible = [item for item in initial
                if item["correctness"]["passed"] and item.get("improvement")]
    ranked = sorted(eligible,
                    key=lambda x: (x.get("improvement") or {}).get("point", 0), reverse=True)
    if not ranked:
        raise RuntimeError("no correct candidate reached final confirmation")
    finalists = ranked[:min(len(ranked), sampling.get("finalists", 2))]
    by_id = {item["id"]: item for item in candidates}
    chosen = [by_id[item["id"]] for item in finalists]
    rows = {base["id"]: []} | {item["id"]: [] for item in chosen}
    stop = "maximum-pairs"
    gates = {}
    adaptive = sampling.get("mode") == "adaptive"
    selection = None
    for pair in range(sampling.get("final_max_pairs", 15)):
        order = [base, *chosen]
        if pair % 2:
            order = list(reversed(order))
        for item in order:
            rows[item["id"]].append(run_command(
                item, f"final-{pair:02d}-{item['id']}", pair, plan, run_dir, variables))
        if pair + 1 < sampling.get("final_min_pairs", 5):
            continue
        direction = plan["metric"].get("direction", "lower")
        if adaptive:
            chosen.sort(key=lambda item: median(x["metric"] for x in rows[item["id"]]),
                        reverse=direction != "lower")
        winner = chosen[0]
        base_cmp = improvement([x["metric"] for x in rows[base["id"]]],
                               [x["metric"] for x in rows[winner["id"]]], direction,
                               sampling.get("bootstrap_iterations", 2000), SEED + 700)
        wall_cmp = improvement([x["wall_s"] for x in rows[base["id"]]],
                               [x["wall_s"] for x in rows[winner["id"]]], "lower",
                               sampling.get("bootstrap_iterations", 2000), SEED + 701)
        separation = None
        if len(chosen) > 1:
            runner = chosen[1]
            separation = improvement([x["metric"] for x in rows[runner["id"]]],
                                     [x["metric"] for x in rows[winner["id"]]], direction,
                                     sampling.get("bootstrap_iterations", 2000), SEED + 702)
        threshold = sampling.get("practical_threshold", 1.02)
        wall_ok = wall_cmp["ci95"][0] > 1 / threshold
        wall_mode = sampling.get("wall_gate", "advisory")
        gates = {
            "correctness": all(row["oracle"]["passed"] for values in rows.values() for row in values),
            "practical_improvement": base_cmp["ci95"][0] >= threshold,
            "winner_separated": separation is None or separation["ci95"][0] > 1.0,
            "no_wall_regression_over_threshold": wall_ok if wall_mode == "strict" else True,
        }
        if adaptive:
            base_precision = precision_envelope(rows[base["id"]], rows[winner["id"]], direction, sampling)
            runner_precision = (precision_envelope(rows[chosen[1]["id"]], rows[winner["id"]], direction, sampling)
                                if len(chosen) > 1 else None)
            separated = separation is None or (separation["ci95"][0] > 1.0 and runner_precision[0] > 1.0)
            equivalent = separation is not None and (
                min(separation["ci95"][0], runner_precision[0]) >= 1 / threshold and
                max(separation["ci95"][1], runner_precision[1]) <= threshold)
            precision_tie = (runner_precision is not None and runner_precision[0] <= 1 <= runner_precision[1]
                             and (equivalent or (separation["ci95"][0] >= 1 / threshold and
                                                separation["ci95"][1] <= threshold) or
                                  separation["ci95"][0] > 1))
            # A reproducible timing tie can verify improvement over baseline, but
            # must never be presented as a uniquely fastest candidate.
            gates.pop("winner_separated")
            gates["finalists_resolved"] = separated or equivalent or precision_tie
            gates["reporting_precision_supports_improvement"] = base_precision[0] >= threshold
            selection = {"unique_winner": separated if separation is not None else None,
                         "comparison_scope": "finalists" if separation is not None else "screened-leader-vs-baseline",
                         "practically_equivalent": equivalent,
                         "indistinguishable_at_reporting_precision": precision_tie,
                         "baseline_precision_envelope": base_precision,
                         "runner_precision_envelope": runner_precision,
                         "basis": "independent-final-pairs", "profiled_times_used": False}
        if all(gates.values()):
            stop = "all-gates-resolved"
            break
        if sampling.get("mode") == "adaptive" and base_cmp["ci95"][1] < threshold:
            stop = "no-practical-improvement"
            break
        if adaptive and not gates["finalists_resolved"]:
            quantum = max((r.get("reporting_quantum") or 0 for values in rows.values() for r in values), default=0)
            best_time = median(r["metric"] for r in rows[winner["id"]])
            if quantum > 0 and best_time <= 20 * quantum:
                stop = "higher-reporting-precision-required"
                break
    winner = chosen[0]
    result = {
        "winner": winner["id"], "runner_up": chosen[1]["id"] if len(chosen) > 1 else None,
        "pairs": len(rows[base["id"]]), "stop": stop, "gates": gates,
        "baseline_vs_winner": improvement([x["metric"] for x in rows[base["id"]]],
                                          [x["metric"] for x in rows[winner["id"]]],
                                          plan["metric"].get("direction", "lower"),
                                          sampling.get("bootstrap_iterations", 2000), SEED + 700),
        "wall_baseline_vs_winner": improvement([x["wall_s"] for x in rows[base["id"]]],
                                               [x["wall_s"] for x in rows[winner["id"]]], "lower",
                                               sampling.get("bootstrap_iterations", 2000), SEED + 701),
        "advisory": {"wall_nonregression_observed": wall_ok,
                     "wall_gate_mode": sampling.get("wall_gate", "advisory")},
        "samples": rows,
        **({"selection": selection} if adaptive else {}),
        "execution_budget": {"timed_executions": sum(len(v) for v in rows.values()),
                             "maximum_executions": sampling.get("final_max_pairs", 15) * (1 + len(chosen))},
    }
    if len(chosen) > 1:
        result["runner_up_vs_winner"] = improvement(
            [x["metric"] for x in rows[chosen[1]["id"]]],
            [x["metric"] for x in rows[winner["id"]]],
            plan["metric"].get("direction", "lower"),
            sampling.get("bootstrap_iterations", 2000), SEED + 702)
    return result


def screening_decision(results, plan):
    """Select the best screened candidate without spending finalist-confirmation runs."""
    eligible = [item for item in results
                if item["correctness"]["passed"] and item.get("improvement")]
    ranked = sorted(eligible,
                    key=lambda item: item["improvement"]["point"], reverse=True)
    if not ranked:
        raise RuntimeError("no correct candidate completed screening")
    winner = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    threshold = plan["sampling"].get("practical_threshold", 1.02)
    return {
        "winner": winner["id"], "runner_up": runner["id"] if runner else None,
        "pairs": winner["pairs"], "stop": "screen-complete",
        "gates": {
            "correctness": winner["correctness"]["passed"],
            "practical_improvement": (winner["improvement"]["point"] >= threshold if winner.get("confidence") == "bounded-unpaired" else
                                      winner["improvement"]["ci95"][0] >= threshold),
        },
        "baseline_vs_winner": winner["improvement"],
        "wall_baseline_vs_winner": winner["wall_improvement"],
        "samples": winner["samples"],
    }
