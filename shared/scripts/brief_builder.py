#!/usr/bin/env python3
"""Assemble ProfileWeaver's compact decision brief from normalized evidence."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from brief_facts import (
    choose_phase,
    classify_iteration,
    details_for,
    fmt_ms,
    load,
    ncu_capsule,
    source_features,
    timeline_shape,
)
from brief_render import render
from kernel_evidence import agent_ncu, attach_advice
from terminal_view import terminal_ncu

PROTOCOL = "profileweaver.agent-brief/v1"
DEFAULT_MAX_TOKENS = 320
DEFAULT_NCU_MAX_TOKENS = 480


def build(run: Path, phase="auto", max_tokens=DEFAULT_MAX_TOKENS):
    request = load(run / "request.json", {})
    profile = load(run / "profile.baseline.json", {})
    report = load(run / "report.json", {})
    harness = load(run / "harness-result.json", {})
    fingerprint = load(run / "baseline-fingerprint.json", {})
    targets_doc = load(run / "source-targets.json", {})
    targets = targets_doc.get("targets") or []
    state = load(run / "experiment-state.json", {})
    iterations = load(run / "iterations.json", [])
    campaign = load(run / "campaign-result.json", {})
    state_iterations = []
    for index, item in enumerate((state.get("candidates") or {}).values(), 1):
        result = item.get("result") or {}
        if not result:
            continue
        metric = result.get("metric") or {}
        state_iterations.append({
            "iteration": index, "optimization": item.get("description") or item.get("id"),
            "mechanism": item.get("mechanism"),
            "result": {"primary": {"median": metric.get("median"),
                                    "unit": (request.get("metric") or {}).get("unit"),
                                    "improvement": (result.get("improvement") or {}).get("point")}},
        })
    if state_iterations:
        iterations = state_iterations
    if not isinstance(iterations, list):
        iterations = []
    details = details_for(run, profile)
    shape = timeline_shape(profile, details)
    baseline_targets = [item for item in targets
                        if (item.get("dependency") or {}).get("relation") != "existing-variant"]
    variant_targets = [item for item in targets
                       if (item.get("dependency") or {}).get("relation") == "existing-variant"]
    # Reduce the complete bounded frontier to facts before it reaches the agent.
    features, scanned_targets = source_features(run, baseline_targets + variant_targets)
    metric_name = str((request.get("metric") or {}).get("name") or "").lower()
    whole_metric = any(token in metric_name for token in ("whole", "wall", "end-to-end", "total"))
    timeline_signals = []
    if 'setup-copy-heavy' in shape.get('labels', []):
        timeline_signals.append("setup-transfer-heavy" if whole_metric else "setup-transfer-collateral")
    advice_features = features + timeline_signals
    history = Counter()
    for item in iterations:
        for label in classify_iteration(item):
            history[label] += 1
    ncu = ncu_capsule(run, profile, advice_features)
    if ncu:
        ncu = attach_advice(ncu, ncu.get("hotspot_fraction"), dict(history),
                            (state.get("best") or {}).get("improvement"), advice_features)
    latest = []
    last_batch = (state.get("batches") or [{}])[-1]
    baseline_ns = (((state.get("kernel_profiles") or {}).get("baseline") or {})
                   .get("duration_ns"))
    for candidate_id in (last_batch.get("candidates") or [])[:3]:
        item = (state.get("candidates") or {}).get(candidate_id) or {}
        result = item.get("result") or {}
        kernel = (item.get("profile") or {}).get("ncu") or {}
        duration = kernel.get("duration_ns")
        latest.append({
            "id": candidate_id,
            "roi_speedup": (result.get("improvement") or {}).get("point"),
            "roi_ci95": (result.get("improvement") or {}).get("ci95"),
            "correct": (result.get("correctness") or {}).get("passed"),
            "mechanism_kernel_speedup": (baseline_ns / duration
                                         if baseline_ns and duration else None),
            "ncu": terminal_ncu(kernel),
        })
    # GPA families and measured counters remain in the packet; generated mechanisms and
    # opportunity rankings stay backend-only so they cannot anchor the agent.
    frontier = []
    phase = choose_phase(phase, report, iterations, harness)
    hotspots = profile.get("hotspots") or []
    top = hotspots[0] if hotspots else {}
    totals = profile.get("totals") or {}
    gates = (report or {}).get("gates") or {}
    failed_gates = [name for name, value in gates.items()
                    if (isinstance(value, dict) and not value.get("passed")) or value is False]
    best = None
    campaign_direction = (campaign.get("metric") or {}).get("direction", "lower")
    for item in iterations:
        result = item.get("result") or {}
        primary = result.get("primary") or {}
        roi = result.get("roi") or {}
        value = primary.get("median", roi.get("median_s"))
        better = (best is None or (value is not None and
                  ((value < best["value"]) if campaign_direction == "lower" else (value > best["value"]))))
        if value is not None and better:
            best = {"iteration": item.get("iteration"), "value": value,
                    "unit": primary.get("unit", "s" if roi else None),
                    "improvement": primary.get("improvement", roi.get("speedup"))}
    source_refs = []
    for target in baseline_targets:
        src = target.get("source") or {}
        slc = target.get("slice") or {}
        source_refs.append({"id": target.get("id"), "path": src.get("path"),
                            "lines": [src.get("start_line"), src.get("end_line")],
                            "slice": slc.get("path"),
                            "relation": (target.get("dependency") or {}).get("relation", "hotspot"),
                            "parent": (target.get("dependency") or {}).get("parent"), "scope": (target.get("resolution") or {}).get("scope"),
                            "confidence": (target.get("resolution") or {}).get("confidence")})
    variant_refs = []
    for target in variant_targets:
        src = target.get("source") or {}
        variant_refs.append({"id": target.get("id"), "symbol": target.get("symbol"),
                             "path": src.get("path"),
                             "lines": [src.get("start_line"), src.get("end_line")],
                             "slice": (target.get("slice") or {}).get("path"),
                             "relation": "existing-variant",
                             "parent": (target.get("dependency") or {}).get("parent"), "scope": (target.get("resolution") or {}).get("scope")})
    action = {
        "optimize": "use the measured evidence and named source slices to design a candidate",
        "iterate": "choose and test the highest-value candidate supported by the evidence",
        "repair": f"repair failed stage {harness.get('failed_stage')}",
        "final": "deliver only if every trust gate passes; otherwise repair the failed gate",
    }[phase]
    machine = {
        "protocol": PROTOCOL,
        "run_id": run.name,
        "phase": phase,
        "action": action,
        "measurement": {
            "roi_ms": fmt_ms(totals.get("wall_time_s")),
            "gpu_busy_ms": fmt_ms(totals.get("gpu_busy_s")),
            "top": {"name": top.get("name"), "ms": fmt_ms(top.get("self_time_s")),
                    "roi_pct": top.get("pct_of_wall"), "calls": top.get("calls"),
                    "amdahl_ceiling": top.get("amdahl_ceiling")},
            "timeline_shape": shape,
            "kernel_diagnostics": agent_ncu(ncu),
        },
        "source": {"targets": source_refs, "variants": variant_refs, "facts": features, "signals": timeline_signals,
                   "scanned_emitted_targets": scanned_targets,
                   "policy": "read only listed slices; resolve one named dependency at a time; agent chooses edits"},
        "campaign": {"iterations": len(iterations), "best": best,
                     "latest": latest,
                     "winner": ((state.get("confirmed") or state.get("best") or {}).get("id") or
                                (campaign.get("final") or {}).get("winner")),
                     "confirmation_pairs": (campaign.get("final") or {}).get("pairs"),
                     "confirmation_stop": (campaign.get("final") or {}).get("stop")},
        "candidate_authority": "agent",
        "trust": {"failed_gates": failed_gates, "oracle": request.get("correctness") or request.get("oracle"),
                  "baseline_contract": {"build_command": request.get("build_command") or request.get("build"),
                                        "baseline": request.get("baseline"),
                                        "fingerprint": fingerprint.get("contract_sha256")}},
        "artifacts": {
            "machine": "agent-brief.json", "text": "agent-brief.txt",
            "details": "python3 shared/scripts/brief.py %s --print-json" % run,
            "agent_view": "agent-view.json" if (run / "agent-view.json").exists() else None,
        },
    }

    text, context = render(
        run, phase, action, request, fingerprint, totals, top, shape, source_refs,
        frontier, history, iterations, best, features, ncu, latest, failed_gates,
        variant_refs, max_tokens, DEFAULT_MAX_TOKENS, DEFAULT_NCU_MAX_TOKENS)
    machine["context"] = context
    return machine, text
