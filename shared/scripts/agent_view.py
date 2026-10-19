#!/usr/bin/env python3
"""Build a bounded ProfileWeaver decision packet instead of printing profiler artifacts.

Default use:

    python3 shared/scripts/agent_view.py runs/<run-id>

The complete profile, benchmark, report, and raw profiler files remain on disk.  Stdout is one
small JSON status line; the bounded packet is written to runs/<run-id>/agent-view.json.  Use a
targeted drill-down only when the packet says the missing detail can change the next action:

    python3 shared/scripts/agent_view.py runs/<run-id> --section hotspots --limit 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path


PROTOCOL = "profileweaver.agent-view/v1"
DEFAULT_MAX_BYTES = 4096


def load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def compact_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if value == 0:
        return 0
    return float(f"{value:.7g}")


def clip(text, limit=240):
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def file_ref(run: Path, path: Path):
    if not path.exists() or not path.is_file():
        return None
    try:
        rel = str(path.relative_to(run))
    except ValueError:
        rel = str(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return {"path": rel, "bytes": path.stat().st_size, "sha256_16": digest}


def evidence_bytes(run: Path):
    total = 0
    candidates = list((run / "raw").rglob("*")) if (run / "raw").exists() else []
    candidates += list(run.glob("profile*.json")) + list(run.glob("bench*.json"))
    candidates += [run / "report.json", run / "preflight.json", run / "request.json", run / "harness-result.json"]
    seen = set()
    for path in candidates:
        if path in seen or not path.is_file() or path.is_symlink():
            continue
        seen.add(path)
        total += path.stat().st_size
    return total


def hotspot_view(hotspot):
    return {
        "rank": hotspot.get("rank"),
        "name": clip(hotspot.get("name"), 150),
        "kind": hotspot.get("kind"),
        "self_s": compact_number(hotspot.get("self_time_s")),
        "wall_pct": compact_number(hotspot.get("pct_of_wall")),
        "calls": hotspot.get("calls"),
    }


def profile_view(profile, limit):
    if not profile:
        return None
    totals = profile.get("totals") or {}
    bottleneck = profile.get("bottleneck") or {}
    hot = [hotspot_view(x) for x in (profile.get("hotspots") or [])[:limit]]
    return {
        "phase": profile.get("phase"),
        "wall_s": compact_number(totals.get("wall_time_s")),
        "gpu_busy_s": compact_number(totals.get("gpu_busy_s")),
        "attributed_s": compact_number(totals.get("attributed_s")),
        "bottleneck": bottleneck.get("class"),
        "scope": clip(bottleneck.get("scope"), 160),
        "confidence": bottleneck.get("confidence"),
        "hotspots": hot,
    }


def profile_diff(before, after):
    if not before or not after:
        return None
    bt, at = before.get("totals") or {}, after.get("totals") or {}
    bw, aw = bt.get("wall_time_s"), at.get("wall_time_s")
    bg, ag = bt.get("gpu_busy_s"), at.get("gpu_busy_s")
    by_name = {x.get("name"): x for x in before.get("hotspots") or []}
    after_hot = after.get("hotspots") or []
    emerging, growing = [], []
    for item in after_hot[:12]:
        name = item.get("name")
        prior = by_name.get(name)
        if prior is None:
            emerging.append(clip(name, 100))
            continue
        old, new = prior.get("self_time_s"), item.get("self_time_s")
        if old and new and new > old * 1.05:
            growing.append({"name": clip(name, 90), "before_s": compact_number(old),
                            "after_s": compact_number(new)})
    return {
        "wall_change_pct": compact_number((aw / bw - 1) * 100) if bw and aw is not None else None,
        "gpu_busy_change_pct": compact_number((ag / bg - 1) * 100) if bg and ag is not None else None,
        "emerging_hotspots": emerging[:3],
        "growing_hotspots": growing[:3],
    }


def impact_diff_view(diff, limit):
    if not diff:
        return None
    selected = []
    preferred = [x for x in diff.get("metric_deltas", []) if x.get("metric") == "self_time_s"]
    def small_delta(item):
        value = {"name": clip(item.get("name"), 55), "base": item.get("base"), "opt": item.get("opt"),
                 "change_pct": item.get("change_pct"), "wall_impact_pct": item.get("wall_impact_pct")}
        if item.get("file"):
            value.update({"file": item.get("file"), "line": item.get("line")})
        return value

    for item in (preferred or diff.get("metric_deltas", []))[:1]:
        selected.append({"metric": item.get("metric"),
                         "decreases": [small_delta(x) for x in (item.get("decreases") or [])[:limit]],
                         "increases": [small_delta(x) for x in (item.get("increases") or [])[:limit]]})
    line_selected = []
    for item in diff.get("top_line_deltas", [])[:2]:
        line_selected.append({"metric": item.get("metric"),
                              "decreases": [small_delta(x) for x in (item.get("decreases") or [])[:limit]],
                              "increases": [small_delta(x) for x in (item.get("increases") or [])[:limit]]})
    context = diff.get("context_selection") or {}
    leverage = diff.get("remaining_leverage") or {}
    leverage = {"name": clip(leverage.get("name"), 90), "file": leverage.get("file"), "line": leverage.get("line"),
                "pct_of_variant_wall": leverage.get("pct_of_variant_wall"),
                "amdahl_ceiling": leverage.get("amdahl_ceiling_if_serial_and_removable")} if leverage else None
    return {"headline": diff.get("headline"), "impact_ranked": selected,
            "top_line_deltas": line_selected, "remaining_leverage": leverage,
            "unmatched_counts": {"baseline": context.get("baseline_entities", 0) - context.get("matched_entities", 0),
                                 "variant": context.get("variant_entities", 0) - context.get("matched_entities", 0)},
            "line_attribution_available": context.get("line_attribution_available")}


def benchmark_view(report, benches):
    measurement = (report or {}).get("measurement")
    if measurement:
        return {
            "baseline_s": compact_number(measurement.get("baseline_median_s")),
            "variant_s": compact_number(measurement.get("variant_median_s")),
            "speedup": compact_number(measurement.get("speedup")),
            "ci95": [compact_number(x) for x in measurement.get("speedup_ci95") or []],
            "reps": measurement.get("reps"),
            "interleaved": measurement.get("interleaved"),
        }
    ranked = []
    for path, bench in benches:
        speedup = bench.get("speedup") or {}
        if speedup.get("point") is None:
            continue
        ranked.append((speedup.get("point", 0), path, bench))
    if not ranked:
        return None
    _, path, bench = max(ranked)
    speedup = bench["speedup"]
    return {
        "label": bench.get("label"), "artifact": path.name,
        "variant_s": compact_number(bench.get("median_s")),
        "speedup": compact_number(speedup.get("point")),
        "ci95": [compact_number(x) for x in speedup.get("ci95") or []],
        "significant": speedup.get("significant"), "reps": bench.get("reps"),
        "practical": speedup.get("practically_significant"),
        "stop": (bench.get("stopping") or {}).get("reason"),
        "correctness": (bench.get("correctness") or {}).get("status"),
    }


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
                                "reason": "all trust gates passed", "confidence": "high",
                                "failed_gates": []}
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
        return "profile-ready", {"action": "inspect-target-source-and-propose-one-change",
                                 "target": target, "reason": "baseline profile identified a measurable target",
                                 "confidence": bottleneck.get("confidence"), "failed_gates": []}
    return "needs-profile", {"action": "execute-selected-child", "target": None,
                             "reason": "no normalized baseline profile exists", "confidence": None,
                             "failed_gates": []}


def build_view(run: Path, max_bytes=DEFAULT_MAX_BYTES, output: Path | None = None):
    request = load(run / "request.json") or {}
    report = load(run / "report.json")
    baseline = load(run / "profile.baseline.json")
    variant_path = select_variant_profile(run, report)
    variant = load(variant_path) if variant_path else None
    impact_diff = load(run / "profile-diff.json")
    harness_result = load(run / "harness-result.json")
    benches = [(p, load(p)) for p in sorted(run.glob("bench*.json"))]
    benches = [(p, x) for p, x in benches if isinstance(x, dict)]
    gates, failed = gate_view(report)
    state, decision = state_and_decision(report, baseline, variant, bool(benches), harness_result)
    route = (request.get("routing") or {}).get("selected_child") or (report or {}).get("skill")
    tool = (report or {}).get("tool") or ((baseline or {}).get("tool") or {}).get("name")
    question = request.get("framed_question") or (report or {}).get("question")
    packet_path = output or run / "agent-view.json"
    key_paths = [run / "request.json", run / "profile.baseline.json"]
    if variant_path:
        key_paths.append(variant_path)
    key_paths += [run / "report.json", run / "harness-result.json", run / "cost.json"]
    refs = [x for x in (file_ref(run, p) for p in key_paths) if x]
    limits = []
    for warning in ((variant or baseline or {}).get("warnings") or [])[:4]:
        limits.append(clip(warning.get("detail") if isinstance(warning, dict) else warning, 150))
    limits += [clip(x, 150) for x in ((report or {}).get("limits") or {}).get("cannot_observe", [])[:3]]
    omissions = ["raw profiler output", "full commands", "full hotspot list", "benchmark samples",
                 "gate explanations", "source text outside emitted slices"]
    source_targets = load(run / "source-targets.json") or {}
    run_arg = f"runs/{run.name}" if run.parent.name == "runs" else str(run)

    def source_view(limit):
        items = []
        for target in (source_targets.get("targets") or [])[:limit]:
            source, source_slice = target.get("source") or {}, target.get("slice") or {}
            items.append({
                "id": target.get("id"), "hotspot": clip(target.get("hotspot"), 100),
                "wall_pct": compact_number(target.get("wall_pct")),
                "path": source.get("path"), "lines": [source.get("start_line"), source.get("end_line")],
                "slice": source_slice.get("path"),
                "confidence": (target.get("resolution") or {}).get("confidence"),
            })
        return items

    def assemble(hotspot_limit, ref_limit, limit_limit, question_limit):
        drilldowns = {
            "hotspots": f"python3 shared/scripts/agent_view.py {run_arg} --section hotspots",
            "warnings": f"python3 shared/scripts/agent_view.py {run_arg} --section warnings",
            "benchmarks": f"python3 shared/scripts/agent_view.py {run_arg} --section benchmarks",
            "gates": f"python3 shared/scripts/agent_view.py {run_arg} --section gates",
            "source": f"python3 shared/scripts/agent_view.py {run_arg} --section source",
        }
        if harness_result:
            drilldowns["execution"] = (
                f"python3 shared/scripts/agent_view.py {run_arg} --section harness"
            )
        return {
            "protocol": PROTOCOL,
            "run_id": run.name,
            "state": state,
            "question": clip(question, question_limit),
            "route": {"skill": route, "tool": clip(tool, 120)},
            "decision": decision,
            "code_targets": source_view(min(hotspot_limit, 2)),
            "evidence": {
                "baseline": profile_view(baseline, hotspot_limit),
                "variant": profile_view(variant, hotspot_limit),
                "profile_diff": impact_diff_view(impact_diff, hotspot_limit) or profile_diff(baseline, variant),
                "benchmark": benchmark_view(report, benches),
                "gates": gates,
                "limits": limits[:limit_limit],
            },
            "artifacts": {
                "packet": str(packet_path.relative_to(run)) if packet_path.is_relative_to(run) else str(packet_path),
                "key": refs[:ref_limit],
                "drilldowns": drilldowns,
            },
            "context": {
                "max_bytes": max_bytes, "packet_bytes": 0, "estimated_tokens": 0,
                "token_estimate_method": "ceil(UTF-8 bytes / 4)",
                "evidence_bytes_on_disk": evidence_bytes(run), "reduction_ratio": None,
                "omissions": omissions,
            },
        }

    settings = [(5, 5, 5, 320), (3, 4, 3, 220), (2, 3, 2, 160), (1, 2, 1, 120)]
    packet = None
    def stabilize(candidate):
        seen = set()
        for _ in range(100):
            payload = json.dumps(candidate, separators=(",", ":"), ensure_ascii=False).encode()
            size = len(payload)
            context = candidate["context"]
            full = context["evidence_bytes_on_disk"]
            desired = (size, math.ceil(size / 4), round(full / size, 2) if size and full else None)
            current = (context["packet_bytes"], context["estimated_tokens"], context["reduction_ratio"])
            if current == desired:
                return payload
            if (current, desired) in seen:
                break
            seen.add((current, desired))
            context["packet_bytes"], context["estimated_tokens"], context["reduction_ratio"] = desired
        # Self-referential size metadata can have no exact compact-JSON fixed point: changing a
        # rounded ratio by 0.01 may add one byte, which changes the ratio back. JSON permits
        # trailing whitespace, so find the smallest declared size that contains the document
        # and pad to that exact size. Consumers still parse one object and stat() equals the
        # declared packet_bytes value.
        lower = len(json.dumps(candidate, separators=(",", ":"), ensure_ascii=False).encode())
        for target in range(lower, max(max_bytes, lower + 512) + 1):
            context = candidate["context"]
            full = context["evidence_bytes_on_disk"]
            context["packet_bytes"] = target
            context["estimated_tokens"] = math.ceil(target / 4)
            context["reduction_ratio"] = round(full / target, 2) if target and full else None
            payload = json.dumps(candidate, separators=(",", ":"), ensure_ascii=False).encode()
            if len(payload) <= target:
                return payload + b" " * (target - len(payload))
        raise ValueError("agent packet size metadata could not be represented")

    for args in settings:
        candidate = assemble(*args)
        payload = stabilize(candidate)
        if len(payload) <= max_bytes:
            packet = candidate
            break
    if packet is None:
        raise ValueError(f"essential agent packet exceeds --max-bytes={max_bytes}; raise the budget")
    payload = stabilize(packet)
    if len(payload) > max_bytes:
        raise ValueError(f"serialized agent packet is {len(payload)} bytes, over --max-bytes={max_bytes}")
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = packet_path.with_name(packet_path.name + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(packet_path)
    return packet, packet_path


def drilldown(run: Path, section: str, limit: int):
    report = load(run / "report.json") or {}
    baseline = load(run / "profile.baseline.json") or {}
    variant_path = select_variant_profile(run, report)
    variant = load(variant_path) if variant_path else {}
    if section == "hotspots":
        return {"baseline": (baseline.get("hotspots") or [])[:limit],
                "variant": (variant.get("hotspots") or [])[:limit]}
    if section == "warnings":
        return {"baseline": baseline.get("warnings") or [], "variant": variant.get("warnings") or [],
                "limits": report.get("limits")}
    if section == "benchmarks":
        return {p.name: load(p) for p in sorted(run.glob("bench*.json"))[:limit]}
    if section == "gates":
        return {"status": report.get("status"), "gates": report.get("gates"),
                "blockers": report.get("blockers")}
    if section == "source":
        return load(run / "source-targets.json") or {"targets": [], "status": "not-generated"}
    if section == "harness":
        result, state = load(run / "harness-result.json") or {}, load(run / "harness-state.json") or {}
        failed = result.get("failed_stage")
        return {"result": result, "failed_stage": (state.get("stages") or {}).get(failed) if failed else None}
    if section == "artifacts":
        return {"profiles": [str(p.relative_to(run)) for p in sorted(run.glob("profile*.json"))],
                "benchmarks": [str(p.relative_to(run)) for p in sorted(run.glob("bench*.json"))],
                "raw": [str(p.relative_to(run)) for p in sorted((run / "raw").rglob("*")) if p.is_file()] if (run / "raw").exists() else []}
    raise ValueError(section)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--section", choices=["hotspots", "warnings", "benchmarks", "gates", "source", "harness", "artifacts"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--print-packet", action="store_true", help="print the full bounded packet instead of one status line")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    if args.section:
        print(json.dumps(drilldown(run, args.section, args.limit), indent=2, ensure_ascii=False))
        return 0
    packet, path = build_view(run, args.max_bytes, args.output.resolve() if args.output else None)
    if args.print_packet:
        print(json.dumps(packet, separators=(",", ":"), ensure_ascii=False))
    else:
        print(json.dumps({"run_id": packet["run_id"], "state": packet["state"],
                          "action": packet["decision"]["action"],
                          "packet": str(path), "bytes": packet["context"]["packet_bytes"],
                          "estimated_tokens": packet["context"]["estimated_tokens"],
                          "evidence_reduction_ratio": packet["context"]["reduction_ratio"]},
                         separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
