"""Run-state and next-action decisions for the bounded agent packet."""

from __future__ import annotations

from pathlib import Path

from agent_view_profile import clip, compact_number


def evidence_bytes(run: Path):
    candidates = list((run / "raw").rglob("*")) if (run / "raw").exists() else []
    candidates += list(run.glob("profile*.json")) + list(run.glob("bench*.json"))
    candidates += [run / name for name in
                   ["report.json", "preflight.json", "request.json", "harness-result.json"]]
    seen, total = set(), 0
    for path in candidates:
        if path in seen or not path.is_file() or path.is_symlink():
            continue
        seen.add(path)
        total += path.stat().st_size
    return total


def benchmark_view(report, benches):
    measurement = (report or {}).get("measurement")
    if measurement:
        return {"baseline_s": compact_number(measurement.get("baseline_median_s")),
                "variant_s": compact_number(measurement.get("variant_median_s")),
                "speedup": compact_number(measurement.get("speedup")),
                "ci95": [compact_number(x) for x in measurement.get("speedup_ci95") or []],
                "reps": measurement.get("reps"), "interleaved": measurement.get("interleaved")}
    ranked = []
    for path, bench in benches:
        speedup = bench.get("speedup") or {}
        if speedup.get("point") is not None:
            ranked.append((speedup.get("point", 0), path, bench))
    if not ranked:
        return None
    _, path, bench = max(ranked)
    speedup = bench["speedup"]
    return {"label": bench.get("label"), "artifact": path.name,
            "variant_s": compact_number(bench.get("median_s")),
            "speedup": compact_number(speedup.get("point")),
            "ci95": [compact_number(x) for x in speedup.get("ci95") or []],
            "significant": speedup.get("significant"), "reps": bench.get("reps"),
            "practical": speedup.get("practically_significant"),
            "stop": (bench.get("stopping") or {}).get("reason"),
            "correctness": (bench.get("correctness") or {}).get("status")}


def gate_view(report):
    if not report or not report.get("gates"):
        return None, []
    gates, failed = {}, []
    for name, value in report["gates"].items():
        passed = bool((value or {}).get("passed"))
        gates[name] = passed
        if not passed:
            failed.append(name)
    return gates, failed


def select_variant_profile(run: Path, report):
    if report:
        after = (report.get("reprofile") or {}).get("profile_after")
        if after and (run / after).exists():
            return run / after
    for name in ["profile.variant.json", "profile.reprofile.json"]:
        if (run / name).exists():
            return run / name
    candidates = sorted(p for p in run.glob("profile.*.json") if p.name != "profile.baseline.json")
    return candidates[-1] if candidates else None


def state_and_decision(report, baseline, variant=None, has_benchmark=False, harness_result=None):
    if harness_result and harness_result.get("status") in {"failed", "blocked"}:
        failed = harness_result.get("failed_stage")
        return "blocked", {"action": "inspect-failed-stage-log", "target": failed,
                           "reason": f"harness {harness_result.get('status')} at {failed or 'dependency check'}",
                           "confidence": None, "failed_gates": ["execution"]}
    if report:
        status = report.get("status", "proposed")
        failed = [k for k, v in (report.get("gates") or {}).items() if not (v or {}).get("passed")]
        target = (report.get("diagnosis") or {}).get("target")
        if status == "verified":
            return "verified", {"action": "deliver-winner", "target": target,
                                "reason": "all trust gates passed", "confidence": "high", "failed_gates": []}
        if status == "negative":
            return "negative", {"action": "report-negative-result", "target": target,
                                "reason": "no worthwhile verified rewrite remains", "confidence": None,
                                "failed_gates": failed}
        if status == "blocked":
            return "blocked", {"action": "resolve-blocker", "target": target,
                               "reason": clip("; ".join(report.get("blockers") or ["measurement blocked"]), 180),
                               "confidence": None, "failed_gates": failed}
        return "proposed", {"action": "repair-failed-gates-or-escalate", "target": target,
                            "reason": "candidate cannot be trusted yet", "confidence": None,
                            "failed_gates": failed}
    if baseline:
        bottleneck = baseline.get("bottleneck") or {}
        target = bottleneck.get("scope") or ((baseline.get("hotspots") or [{}])[0].get("name"))
        if variant:
            action = "adjudicate-and-write-report" if has_benchmark else "run-correctness-and-paired-benchmark"
            return "candidate-ready", {"action": action, "target": target,
                                       "reason": "baseline and variant profiles exist but trust gates are not adjudicated",
                                       "confidence": bottleneck.get("confidence"), "failed_gates": []}
        alternatives = bottleneck.get("alternatives") or []
        if bottleneck.get("class") == "indeterminate" and alternatives:
            child = alternatives[0].get("suggested_child")
            return "profile-ready", {"action": "escalate", "target": target,
                                     "reason": f"timeline localized the target; use {child} for the missing mechanism",
                                     "confidence": bottleneck.get("confidence"), "failed_gates": []}
        return "profile-ready", {"action": "inspect-target-source-and-propose-one-change", "target": target,
                                 "reason": "baseline profile identified a measurable target",
                                 "confidence": bottleneck.get("confidence"), "failed_gates": []}
    return "needs-profile", {"action": "execute-selected-child", "target": None,
                             "reason": "no normalized baseline profile exists", "confidence": None,
                             "failed_gates": []}
