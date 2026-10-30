"""Assemble and size the bounded ProfileWeaver agent packet."""

from __future__ import annotations

import json
import math
from pathlib import Path

from agent_view_decision import (benchmark_view, evidence_bytes, gate_view,
                                 select_variant_profile, state_and_decision)
from agent_view_profile import (clip, compact_number, file_ref, impact_diff_view, load,
                                profile_diff, profile_view)

PROTOCOL = "profileweaver.agent-view/v1"
DEFAULT_MAX_BYTES = 4096

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
    plan_arg = f"{run_arg}/experiment-plan.json"

    def source_view(limit):
        items = []
        for target in (source_targets.get("targets") or [])[:limit]:
            source, source_slice = target.get("source") or {}, target.get("slice") or {}
            items.append({
                "id": target.get("id"), "hotspot": clip(target.get("hotspot"), 100),
                "wall_pct": compact_number(target.get("wall_pct")),
                "path": source.get("path"), "lines": [source.get("start_line"), source.get("end_line")],
                "slice": source_slice.get("path"),
                "relation": (target.get("dependency") or {}).get("relation", "hotspot"),
                "parent": (target.get("dependency") or {}).get("parent"),
                "confidence": (target.get("resolution") or {}).get("confidence"),
            })
        return items

    def source_frontier():
        return [{"id": target.get("id"),
                 "relation": (target.get("dependency") or {}).get("relation", "hotspot"),
                 "parent": (target.get("dependency") or {}).get("parent")}
                for target in (source_targets.get("targets") or [])]

    def assemble(hotspot_limit, ref_limit, limit_limit, question_limit):
        drilldowns = {
            "hotspots": f"python3 shared/scripts/experiment.py {plan_arg} inspect --section hotspots",
            "warnings": f"python3 shared/scripts/experiment.py {plan_arg} inspect --section warnings",
            "benchmarks": f"python3 shared/scripts/experiment.py {plan_arg} inspect --section benchmarks",
            "gates": f"python3 shared/scripts/experiment.py {plan_arg} inspect --section gates",
            "source": f"python3 shared/scripts/experiment.py {plan_arg} inspect --section source",
        }
        if harness_result:
            drilldowns["execution"] = (
                f"python3 shared/scripts/experiment.py {plan_arg} inspect --section harness"
            )
        return {
            "protocol": PROTOCOL,
            "run_id": run.name,
            "state": state,
            "question": clip(question, question_limit),
            "route": {"skill": route, "tool": clip(tool, 120)},
            "decision": decision,
            "code_targets": source_view(min(hotspot_limit, 2)),
            "source_frontier": source_frontier(),
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
