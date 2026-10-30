#!/usr/bin/env python3
"""Adjudicate a campaign and emit the standard trusted rewrite report."""

import argparse
import json
import statistics as st
import sys
from pathlib import Path

from finalize_support import (REPO, gate, limitations, load, outcome, run_quiet,
                              target_measurement, write)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    plan_path = (args.plan or run / "campaign-plan.json").resolve()
    plan = load(plan_path, {})
    campaign = load(run / "campaign-result.json", {})
    if plan.get("version") != 1 or campaign.get("status") != "complete":
        parser.error("a valid campaign plan and complete campaign-result.json are required")

    request = load(run / "request.json", {})
    before_path, after_path = run / "profile.baseline.json", run / "profile.reprofile.json"
    before, after = load(before_path, {}), load(after_path, {})
    fingerprint = load(run / "baseline-fingerprint.json", {})
    final = campaign["final"]
    winner_id = final["winner"]
    winner_plan = next(item for item in plan["candidates"] if item["id"] == winner_id)
    threshold = plan["sampling"]["practical_threshold"]

    raw_finalize = run / "raw/finalize"
    diff = {}
    if before and after:
        diff_argv = [sys.executable, str(REPO / "shared/scripts/diff_profiles.py"),
                     "--baseline", str(before_path), "--variant", str(after_path),
                     "--output", str(run / "profile-diff.json")]
        if (run / "source-targets.json").exists():
            diff_argv += ["--source-targets", str(run / "source-targets.json")]
        run_quiet(diff_argv, raw_finalize, "profile-diff")
        diff = load(run / "profile-diff.json", {})

    target = target_measurement(plan, before, after)
    comparison = final["baseline_vs_winner"]
    wall_comparison = final["wall_baseline_vs_winner"]
    base_rows = final["samples"][plan["baseline"]["id"]]
    winner_rows = final["samples"][winner_id]
    base_metric = [row["metric"] for row in base_rows]
    winner_metric = [row["metric"] for row in winner_rows]
    direction = plan["metric"].get("direction", "lower")
    absolute_improved = (st.median(winner_metric) < st.median(base_metric)
                         if direction == "lower" else st.median(winner_metric) > st.median(base_metric))
    comparable = diff.get("comparability") or {}
    same_profile_contract = bool(before and after and comparable.get("same_tool") and
                                 comparable.get("all_declared_workload_fields_equal"))
    fingerprint_preserved = (bool(fingerprint.get("contract_sha256")) and
                             campaign.get("fingerprint") == campaign.get("fingerprint_after"))

    gates = {
        "correctness": gate(final["gates"].get("correctness"),
                            f"All {final['pairs']} final pairs passed the declared oracle."),
        "significance": gate(final["gates"].get("practical_improvement") and absolute_improved,
                             f"Improvement {comparison['point']:.6g}x, paired bootstrap CI "
                             f"[{comparison['ci95'][0]:.6g}, {comparison['ci95'][1]:.6g}], threshold {threshold:.6g}."),
        "mechanism": gate(target["moved"],
                          f"Explicit profile target metric {target['metric']} moved from {target['base']} to {target['opt']}; "
                          "no unobserved mechanism is inferred."),
        "no_collateral": gate(final["gates"].get("no_wall_regression_over_threshold"),
                              f"Whole-process ratio CI [{wall_comparison['ci95'][0]:.6g}, "
                              f"{wall_comparison['ci95'][1]:.6g}] excludes a regression larger than the threshold."),
        "attribution": gate(same_profile_contract and target["baseline"] is not None and target["variant"] is not None,
                            "Baseline and winner use comparable normalized profiles and both explicit target selectors matched."),
        "reproducible": gate(fingerprint_preserved,
                             "Baseline contract exists and all declared campaign inputs had identical pre/post fingerprints."),
    }
    verified = all(item["passed"] for item in gates.values())
    profile_tool = before.get("tool") or {}
    tool_name = profile_tool.get("name") if isinstance(profile_tool, dict) else str(profile_tool or "unknown")
    tool_version = profile_tool.get("version") if isinstance(profile_tool, dict) else None
    target_name = ((target["baseline"] or {}).get("name") or
                   ((before.get("hotspots") or [{}])[0]).get("name") or "unresolved")
    target_pct_before = (target["baseline"] or {}).get("pct_of_wall")
    target_pct_after = (target["variant"] or {}).get("pct_of_wall")
    variants = []
    candidate_plan = {item["id"]: item for item in plan["candidates"]}
    for item in campaign["candidates"]:
        declared = candidate_plan[item["id"]]
        variants.append({
            "name": item["id"], "change": declared.get("description") or item["id"],
            "patch": declared.get("patch"), "benchmark": str(run / "campaign-result.json"),
            "speedup": (item.get("improvement") or {}).get("point"),
            "speedup_ci95": (item.get("improvement") or {}).get("ci95"),
            "correctness": "passed" if item["correctness"]["passed"] else "failed",
            "outcome": outcome(item, winner_id, threshold),
            "note": f"adaptive stop={item['stop']}; pairs={item['pairs']}; mechanism={declared.get('mechanism')}",
        })
    unit = plan["metric"].get("unit")
    measurement = {"speedup": comparison["point"], "speedup_ci95": comparison["ci95"],
                   "reps": final["pairs"], "interleaved": True,
                   "noise_floor_rel_mad": (st.median(abs(x - st.median(base_metric)) for x in base_metric) /
                                           st.median(base_metric) if st.median(base_metric) else None),
                   "conditions": {"campaign_fingerprint": campaign["fingerprint"], "metric_unit": unit}}
    if unit in {"s", "sec", "second", "seconds"}:
        measurement |= {"baseline_median_s": st.median(base_metric),
                        "variant_median_s": st.median(winner_metric)}
    report = {
        "run_id": campaign["run_id"],
        "skill": request.get("skill") or "profile-weaver",
        "tool": " ".join(x for x in [tool_name, tool_version] if x) or None,
        "question": request.get("question") or request.get("performance_question") or
                    f"Does the selected candidate improve {plan['metric'].get('name') or 'the declared metric'} under the fixed contract?",
        "status": "verified" if verified else "proposed",
        "diagnosis": {"bottleneck_class": request.get("bottleneck_class") or
                       f"measured {((target['baseline'] or {}).get('kind') or 'hotspot')} cost",
                       "target": target_name, "target_pct_of_wall": target_pct_before,
                       "amdahl_ceiling": (target["baseline"] or {}).get("amdahl_ceiling"),
                       "evidence": [{"kind": "measurement", "profile": str(before_path),
                                     "metric": target["metric"], "value": target["base"]},
                                    {"kind": "source-hypothesis", "candidate": winner_id,
                                     "mechanism": winner_plan.get("mechanism")},
                                    {"kind": "measurement", "profile": str(after_path),
                                     "metric": target["metric"], "value": target["opt"]}],
                       "confidence": "high" if verified else "low"},
        "prediction": None, "variants": variants, "winner": winner_id,
        "measurement": measurement,
        "reprofile": {"target_pct_before": target_pct_before, "target_pct_after": target_pct_after,
                      "target_shrank": target["moved"],
                      "new_bottleneck_class": ((after.get("hotspots") or [{}])[0]).get("name"),
                      "regressions": [], "profile_before": str(before_path), "profile_after": str(after_path)},
        "gates": gates,
        "limits": {"cannot_observe": limitations(before),
                   "assumptions": ["The declared oracle defines correctness."],
                   "would_falsify": "An oracle failure, confidence interval below the threshold, or target cost that does not move."},
        "next_lever": diff.get("remaining_leverage"),
        "cost_actual": {"benchmark_runs": sum(2 * item["pairs"] for item in campaign["candidates"]) +
                                           final["pairs"] * len(final["samples"]),
                        "wall_clock_s": campaign.get("elapsed_s")},
        "verification_checks": {"correctness": gates["correctness"]["passed"],
                                "paired_improvement": gates["significance"]["passed"],
                                "absolute_metric_improved": absolute_improved,
                                "reprofiled_target_moved": gates["mechanism"]["passed"],
                                "no_material_wall_regression": gates["no_collateral"]["passed"],
                                "baseline_fingerprint_preserved": gates["reproducible"]["passed"]},
    }
    write(run / "report.json", report)
    gate_lines = "\n".join(f"- {name}: {'PASS' if value['passed'] else 'FAIL'} — {value['detail']}"
                           for name, value in gates.items())
    (run / "REPORT.md").write_text(
        f"# ProfileWeaver report: {campaign['run_id']}\n\n"
        f"Status: **{report['status']}**. Winner: `{winner_id}`. Improvement: "
        f"**{comparison['point']:.4g}x** (95% CI {comparison['ci95'][0]:.4g}–{comparison['ci95'][1]:.4g}).\n\n"
        f"## Trust gates\n\n{gate_lines}\n\n"
        f"Detailed evidence: `campaign-result.json`, `profile-diff.json`, and `report.json`.\n")

    schema_rc = run_quiet([sys.executable, str(REPO / "shared/scripts/validate.py"),
                           str(REPO / "shared/schema/rewrite-report.schema.json"),
                           str(run / "report.json")], raw_finalize, "validate-report")
    brief_rc = run_quiet([sys.executable, str(REPO / "shared/scripts/brief.py"), str(run),
                          "--phase", "final"], raw_finalize, "refresh-brief")
    if schema_rc or brief_rc:
        print(json.dumps({"status": "failed", "report": str(run / "report.json"),
                          "schema_rc": schema_rc, "brief_rc": brief_rc}, separators=(",", ":")))
        return 1
    print(json.dumps({"status": report["status"], "winner": winner_id,
                      "improvement": comparison["point"], "pairs": final["pairs"],
                      "gates_passed": sum(x["passed"] for x in gates.values()),
                      "gates_total": len(gates), "report": str(run / "report.json"),
                      "brief": str(run / "agent-brief.txt")}, separators=(",", ":")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
