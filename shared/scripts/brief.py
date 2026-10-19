#!/usr/bin/env python3
"""Build ProfileWeaver's workload-agnostic, decision-only agent brief.

Full profiles, commands, samples, hashes, and source remain in the evidence plane.  The text
brief contains only facts and hypotheses that can change the next optimization decision.  The
JSON sidecar is machine-readable and is not a default agent input.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
PROTOCOL = "profileweaver.agent-brief/v1"
DEFAULT_MAX_TOKENS = 320
DEFAULT_NCU_MAX_TOKENS = 480


def load(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def resolve_artifact(run: Path, value):
    if not value:
        return None
    path = Path(value)
    for candidate in (path, run / path, REPO / path):
        if candidate.exists():
            return candidate.resolve()
    return None


def token_counter():
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("o200k_base")
        return lambda text: len(encoding.encode(text)), "tiktoken o200k_base"
    except Exception:
        return lambda text: math.ceil(len(text.encode()) / 4), "ceil(UTF-8 bytes / 4) fallback"


def fmt_ms(seconds):
    return None if seconds is None else round(seconds * 1000, 3)


def format_oracle(oracle):
    """Render the common request oracle shapes without dumping a full regex."""
    if not oracle:
        return "MISSING"
    if not isinstance(oracle, dict):
        return str(oracle)
    if oracle.get("oracle"):
        return str(oracle["oracle"])
    kind = str(oracle.get("type") or oracle.get("kind") or "oracle")
    if oracle.get("expected") is not None:
        return f"{kind}:{oracle['expected']}"
    if oracle.get("value") is not None:
        return f"{kind}:{oracle['value']}"
    if oracle.get("regex"):
        regex = str(oracle["regex"]).replace("\n", " ")
        return f"{kind}:/{regex[:80]}/"
    return kind


def classify_iteration(item):
    # Classify the variable actually changed. Predictions often mention costs that were merely
    # held inside the ROI and would inflate the apparent search diversity.
    declared = item.get("mechanism")
    if declared:
        return [str(declared)]
    text = str(item.get("optimization", "")).lower()
    matches = []
    rules = [
        ("invariant-precompute", r"precomput|reciprocal|invariant|hoist"),
        ("search-structure", r"directory|bounded|index table|bin(?:s|ning)?|binary search|lower.bound"),
        ("specialization", r"specializ|compile.time|fast path"),
        ("data-ordering", r"sort|radix|key packing|group|reorder"),
        ("launch-geometry", r"threads per block|block size|thread[s]?|launch.bounds"),
        ("allocation-lifetime", r"alloc|free|resident|reuse buffer"),
        ("fusion", r"fus|combined kernel|single kernel"),
        ("math-reduction", r"verification reduction|checksum reduction|arithmetic precision"),
    ]
    for label, pattern in rules:
        if re.search(pattern, text):
            matches.append(label)
    return matches or ["other"]


def source_features(run: Path, targets):
    text = ""
    used = []
    for target in targets:
        path = resolve_artifact(run, (target.get("slice") or {}).get("path"))
        if path:
            text += "\n" + path.read_text(errors="replace")
            used.append(target.get("id"))
    checks = [
        ("exact-search", r"\b(binary_search|lower_bound|upper_bound|equal_range)\b|\bmid\s*=.*(?:low|left).*(?:high|right)"),
        ("state-transition", r"\b(?:rng|random|xorshift|philox|curand)[A-Za-z0-9_]*\b"),
        ("nested-or-repeated-loop", r"\b(?:for|while)\s*\([^\n]+\).*\b(?:for|while)\s*\("),
        ("accumulation-or-reduction", r"(?:\+=|\*=|atomicAdd|\breduce(?:_if)?\s*\()"),
        ("indirect-memory-access", r"\[[^\]\n]*\[[^\]\n]+\][^\]\n]*\]"),
        ("allocation-in-target", r"\b(?:malloc|calloc|realloc|new|cudaMalloc|hipMalloc|omp_target_alloc)\s*[<(]"),
        ("explicit-synchronization", r"\b(?:cuda|hip)(?:Device|Stream)Synchronize\s*\(|\b(?:barrier|taskwait)\b"),
        ("host-device-transfer", r"\b(?:cuda|hip)Memcpy(?:Async)?\s*\(|\bomp_target_memcpy\s*\("),
        ("atomic-update", r"\b(?:atomic[A-Za-z0-9_]*|#pragma\s+omp\s+atomic)\b"),
        ("sort-or-partition", r"\b(?:sort|stable_sort|partition|DeviceRadixSort|sort_by_key)\s*(?:::|\()"),
    ]
    facts = [name for name, pattern in checks if re.search(pattern, text, re.S)]
    if len(re.findall(r"\b(?:if|switch)\s*\(", text)) >= 3:
        facts.append("branch-dense-target")
    return facts, used


def details_for(run: Path, profile):
    for raw in (profile or {}).get("raw_artifacts") or []:
        path = resolve_artifact(run, raw)
        if path and path.name.endswith("metrics.json"):
            return load(path, {})
    candidates = sorted(run.glob("raw/nsys-baseline/*metrics.json"))
    return load(candidates[0], {}) if candidates else {}


def timeline_shape(profile, details):
    totals = (profile or {}).get("totals") or {}
    wall = totals.get("wall_time_s") or 0
    busy = totals.get("gpu_busy_s")
    hotspots = (profile or {}).get("hotspots") or []
    top = hotspots[0] if hotspots else {}
    gpu = details.get("gpu") or {}
    top_share = top.get("pct_of_wall") or 0
    launch_count = gpu.get("kernel_launches")
    copy = gpu.get("copy_union_s") or 0
    labels = []
    if top_share >= 60:
        labels.append("dominant-long-kernel")
    if wall and isinstance(busy, (int, float)) and busy / wall < 0.5:
        labels.append("gpu-gap-heavy")
    if wall and copy / wall >= 0.2:
        labels.append("copy-heavy")
    if launch_count is not None and launch_count >= 100 and gpu.get("kernel_duration_s", {}).get("median", 0) < 50e-6:
        labels.append("fragmented-small-kernels")
    if not labels:
        labels.append("mixed-timeline")
    return {
        "labels": labels,
        "gpu_busy_pct": round(busy / wall * 100, 2) if wall and isinstance(busy, (int, float)) else None,
        "copy_pct": round(copy / wall * 100, 2) if wall else None,
        "kernel_launches": launch_count,
        "top_launch": (details.get("top_launches") or [None])[0],
    }


def ncu_capsule(run: Path, profile):
    """Return only decision-bearing NCU facts; never copy the full counter table."""
    diagnostics = load(run / "kernel-diagnostics.json", {})
    if not diagnostics:
        diagnostics = (profile or {}).get("kernel_diagnostics") or {}
    kernels = diagnostics.get("kernels") or []
    if not kernels:
        return None
    kernel = kernels[0]

    def value(section, name):
        return ((kernel.get(section) or {}).get(name) or {}).get("value")

    timing = ((diagnostics.get("collection") or {}).get("timing") or {})
    requested = ((diagnostics.get("collection") or {}).get("requested") or {})
    return {
        "kernel": kernel.get("name"),
        "requested_section": requested.get("section"),
        "capture_s": timing.get("capture_elapsed_s"), "export_s": timing.get("export_elapsed_s"),
        "total_s": timing.get("total_elapsed_s"),
        "grid_threads": value("launch", "grid_threads"),
        "block_threads": value("launch", "block_threads"),
        "registers_per_thread": value("launch", "registers_per_thread"),
        "static_shared_bytes": value("launch", "static_shared_bytes"),
        "dynamic_shared_bytes": value("launch", "dynamic_shared_bytes"),
        "waves_per_sm": value("launch", "waves_per_sm"),
        "occupancy_pct": value("occupancy", "achieved_pct"),
        "theoretical_occupancy": value("occupancy", "theoretical"),
        "occupancy_limiters": [{"resource": item.get("resource"), "value": item.get("value")}
                               for item in (kernel.get("occupancy") or {}).get("limiters", [])],
        "active_warps": value("scheduler", "active_warps_per_scheduler"),
        "eligible_warps": value("scheduler", "eligible_warps_per_scheduler"),
        "issued_warps": value("scheduler", "issued_warps_per_scheduler"),
        "issue_active_pct": value("scheduler", "issue_active_pct"),
        "sm_throughput_pct": value("throughput", "sm_pct"),
        "compute_memory_throughput_pct": value("throughput", "compute_memory_pct"),
        "dram_throughput_pct": value("throughput", "dram_pct"),
        "l1_throughput_pct": value("throughput", "l1_pct"),
        "l2_throughput_pct": value("throughput", "l2_pct"),
        "l1_hit_pct": value("memory", "l1_hit_pct"),
        "l2_hit_pct": value("memory", "l2_hit_pct"),
        "global_load_bytes_per_sector": value("memory", "global_load_bytes_per_sector"),
        "global_store_bytes_per_sector": value("memory", "global_store_bytes_per_sector"),
        "spill_requests_pct": value("memory", "local_spill_requests_pct"),
        "shared_bank_conflicts": value("memory", "shared_bank_conflicts"),
        "instructions": value("instructions", "executed"),
        "branch_instruction_pct": value("instructions", "branch_instruction_pct"),
        "threads_per_instruction": value("instructions", "threads_executed_per_instruction"),
        "top_compute_pipes": [{"pipe": item.get("pipe"), "value": item.get("value")}
                              for item in (kernel.get("instructions") or {}).get("top_compute_pipes", [])],
        "stalls": [{"reason": item.get("reason"), "value": item.get("value")}
                   for item in (kernel.get("warp_stalls") or [])[:3]],
        "signals": [item.get("kind") for item in (kernel.get("signals") or [])[:3]],
        "rules": [{"name": item.get("name"), "estimated_speedup": item.get("estimated_speedup")}
                  for item in (diagnostics.get("rules") or [])[:3]],
        "source_hotspots": (diagnostics.get("source_hotspots") or [])[:2],
        "raw_metric_rows": (diagnostics.get("context") or {}).get("raw_metric_rows"),
        "drilldowns": diagnostics.get("drilldowns") or {},
        "artifact": "kernel-diagnostics.json",
    }


def build_frontier(features, shape, history_counts):
    candidates = []

    def add(mechanism, trigger, hypothesis, priority):
        candidates.append({"mechanism": mechanism, "trigger": trigger,
                           "hypothesis": hypothesis, "priority": priority,
                           "trials": history_counts.get(mechanism, 0)})

    if "exact-search" in features:
        add("search-structure", "target performs exact search", "bound, index, or amortize the search while preserving exact results", 1)
    if "state-transition" in features:
        add("state-amortization", "target advances state", "share or precompute invariant transitions while preserving sequence semantics", 1)
    if "nested-or-repeated-loop" in features:
        add("invariant-precompute", "target contains nested or repeated iteration", "hoist repeated invariant work or reuse an exact intermediate", 1)
    if "indirect-memory-access" in features:
        add("data-ordering", "target uses indirect addressing", "reorder independent work or data to improve measured target locality", 1)
    if "branch-dense-target" in features:
        add("specialization", "target has several control decisions", "specialize a measured common mode while preserving a generic path", 1)
    if "accumulation-or-reduction" in features:
        add("reduction-structure", "target accumulates or reduces values", "remove redundant accumulation work or restructure the reduction without changing its numerical contract", 2)
    if "allocation-in-target" in features:
        add("allocation-lifetime", "target slice allocates inside the ROI", "reuse or move temporary storage while preserving ROI semantics", 2)
    if "host-device-transfer" in features or "copy-heavy" in shape["labels"]:
        add("data-residency", "measured scope or target contains transfers", "remove redundant movement or overlap an independent transfer", 1)
    if "explicit-synchronization" in features or "gpu-gap-heavy" in shape["labels"]:
        add("synchronization-scope", "timeline or target contains wait gaps", "remove or narrow a provably redundant synchronization", 1)
    if "atomic-update" in features:
        add("contention-reduction", "target uses atomic updates", "privatize or aggregate updates while preserving ordering requirements", 2)
    if "sort-or-partition" in features:
        add("key-domain-or-partition", "target sorts or partitions data", "reduce key domain, passes, or moved bytes while preserving order requirements", 2)
    if "fragmented-small-kernels" in shape["labels"]:
        add("fusion", "timeline has many short kernels", "reduce launch and synchronization count", 1)
    if "dominant-long-kernel" in shape["labels"]:
        add("kernel-work-reduction", "one operation dominates the measured ROI", "remove repeated work inside that operation using only source-supported invariants", 2)
    add("parameter-tuning", "a measured target remains after structural trials", "tune a parameter only when measured evidence identifies the constrained resource", 3)
    # Prefer unexplored structural mechanisms, then evidence priority, then fewer trials.
    candidates.sort(key=lambda item: (item["trials"] > 0, item["priority"], item["trials"], item["mechanism"]))
    return candidates


def choose_phase(requested, report, iterations, harness):
    if requested != "auto":
        return requested
    if report and report.get("status") in {"verified", "accepted", "complete"}:
        return "final"
    if iterations:
        return "iterate"
    if harness and harness.get("status") not in {None, "complete"}:
        return "repair"
    return "optimize"


def build(run: Path, phase="auto", max_tokens=DEFAULT_MAX_TOKENS):
    request = load(run / "request.json", {})
    profile = load(run / "profile.baseline.json", {})
    report = load(run / "report.json", {})
    harness = load(run / "harness-result.json", {})
    fingerprint = load(run / "baseline-fingerprint.json", {})
    targets_doc = load(run / "source-targets.json", {})
    targets = targets_doc.get("targets") or []
    iterations = load(run / "iterations.json", [])
    campaign = load(run / "campaign-result.json", {})
    if not isinstance(iterations, list):
        iterations = []
    details = details_for(run, profile)
    shape = timeline_shape(profile, details)
    ncu = ncu_capsule(run, profile)
    # The first targets are the profiler-selected hotspot and explicitly resolved dependencies.
    # Later targets may belong to experimental variants and must not contaminate baseline facts.
    features, scanned_targets = source_features(run, targets[:3])
    history = Counter()
    for item in iterations:
        for label in classify_iteration(item):
            history[label] += 1
    frontier = build_frontier(features, shape, history)
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
                    "improvement": primary.get("improvement", roi.get("speedup")),
                    "mechanisms": classify_iteration(item)}
    top_launch = shape.get("top_launch") or {}
    source_refs = []
    for target in targets[:2]:
        src = target.get("source") or {}
        slc = target.get("slice") or {}
        source_refs.append({"id": target.get("id"), "path": src.get("path"),
                            "lines": [src.get("start_line"), src.get("end_line")],
                            "slice": slc.get("path"),
                            "confidence": (target.get("resolution") or {}).get("confidence")})
    action = {
        "optimize": "propose a batch of 3 orthogonal structural variants",
        "iterate": "test an unexplored structural mechanism before another parameter sweep",
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
            "kernel_diagnostics": ncu,
        },
        "source": {"targets": source_refs, "facts": features,
                   "scanned_emitted_targets": scanned_targets,
                   "policy": "read only listed slices; resolve one named dependency at a time"},
        "campaign": {"iterations": len(iterations), "mechanism_trials": dict(history),
                     "distinct_mechanisms": len(history), "best": best,
                     "winner": (campaign.get("final") or {}).get("winner"),
                     "confirmation_pairs": (campaign.get("final") or {}).get("pairs"),
                     "confirmation_stop": (campaign.get("final") or {}).get("stop")},
        "frontier": frontier,
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

    launch_text = ""
    if top_launch:
        grid = "x".join(map(str, top_launch.get("grid") or []))
        block = "x".join(map(str, top_launch.get("block") or []))
        launch_text = f" grid={grid} block={block}"
    target_text = ", ".join(f"{x['id']}={Path(x['path']).name}:{x['lines'][0]}-{x['lines'][1]}"
                            for x in source_refs if x.get("path"))
    frontier_text = "; ".join(f"{x['mechanism']}[{x['trials']}]: {x['hypothesis']}"
                              for x in frontier[:3])
    overused = [f"{name}={count}" for name, count in history.most_common() if count >= 3]
    code_facts = ",".join(features) or "none detected in emitted slices"
    oracle = request.get("correctness") or request.get("oracle") or {}
    oracle_text = format_oracle(oracle)
    best_text = (f"i{best['iteration']} {best['value']:.6g} {best.get('unit') or ''} "
                 f"{round(best['improvement'],3)}x"
                 if best and best.get("improvement") is not None else str(best))
    gpu_measure = (f" GPU={fmt_ms(totals.get('gpu_busy_s'))}ms"
                   if totals.get("gpu_busy_s") is not None else "")
    ncu_lines = []
    if ncu:
        stalls = ",".join(f"{item['reason']}={item['value']:.3g}"
                          for item in ncu["stalls"] if item.get("value") is not None) or "none"
        limits = ",".join(f"{item['resource']}={item['value']:.3g}" for item in ncu["occupancy_limiters"]
                          if item.get("value") is not None) or "none"
        pipes = ",".join(f"{item['pipe']}={item['value']:.3g}" for item in ncu["top_compute_pipes"]
                         if item.get("value") is not None) or "none"
        source = ncu["source_hotspots"][0] if ncu["source_hotspots"] else {}
        source_text = ((source.get("instruction") or "").replace(" ", "_") +
                       f"/not-issued={source.get('not_issued_samples')}") if source else "none"
        rules = ",".join(f"{item.get('name')}={item.get('estimated_speedup')}%"
                         for item in ncu["rules"] if item.get("name")) or "none"
        ncu_lines = [
            (f"NCU: all→full,1 launch; cost c/e/t={ncu.get('capture_s')}/"
             f"{ncu.get('export_s')}/{ncu.get('total_s')}s; metrics={ncu.get('raw_metric_rows')}."),
            (f"NCU RES: grid/block={ncu.get('grid_threads')}/{ncu.get('block_threads')} "
             f"r/th={ncu.get('registers_per_thread')} sh={ncu.get('static_shared_bytes')}+"
             f"{ncu.get('dynamic_shared_bytes')}B waves/SM={ncu.get('waves_per_sm')} "
             f"occ={ncu.get('occupancy_pct')}/{ncu.get('theoretical_occupancy')}% lim={limits}."),
            (f"NCU ISSUE: warp a/e/i={ncu.get('active_warps')}/{ncu.get('eligible_warps')}/"
             f"{ncu.get('issued_warps')} issue={ncu.get('issue_active_pct')}% "
             f"stall={stalls} SASS={source_text}."),
            (f"NCU MEM: thr sm/cm/dr/l1/l2={ncu.get('sm_throughput_pct')}/"
             f"{ncu.get('compute_memory_throughput_pct')}/{ncu.get('dram_throughput_pct')}/"
             f"{ncu.get('l1_throughput_pct')}/{ncu.get('l2_throughput_pct')}% hit l1/l2="
             f"{ncu.get('l1_hit_pct')}/{ncu.get('l2_hit_pct')}% B/sector ld/st="
             f"{ncu.get('global_load_bytes_per_sector')}/{ncu.get('global_store_bytes_per_sector')} "
             f"spill/bank={ncu.get('spill_requests_pct')}/{ncu.get('shared_bank_conflicts')}."),
            (f"NCU INST: n={ncu.get('instructions')} br%={ncu.get('branch_instruction_pct')} "
             f"th/inst={ncu.get('threads_per_instruction')} pipe={pipes}; rule-est={rules}."),
        ]
    header = (f"PW4 {phase} run={run.name}; test ≤3 distinct candidates."
              if ncu else f"PW4 {phase} run={run.name}; ACTION: {action}.")
    read_line = (f"READ ONLY: {target_text or 'source mapping unresolved'}; +1 named dependency if blocked."
                 if ncu else
                 f"READ: {target_text or 'source mapping unresolved'}; only these slices, then one named dependency if required.")
    frontier_line = (f"NEXT hypotheses: {','.join(item['mechanism'] for item in frontier[:3])}; one mechanism/candidate."
                     if ncu else
                     f"FRONTIER (hypotheses): {frontier_text}. Batch distinct mechanisms before tuning.")
    verify_line = (f"GATE: fp={(fingerprint.get('contract_sha256') or 'MISSING')[:12]} "
                   f"oracle={oracle_text}; paired+reprofile; failed={failed_gates or 'none'}."
                   if ncu else
                   f"VERIFY: baseline_fp={(fingerprint.get('contract_sha256') or 'MISSING')[:12]}; oracle={oracle_text}; "
                   f"one variable; paired benchmark; reprofile winner; failed_gates={failed_gates or 'none'}.")
    measure_line = (f"TIME: ROI/GPU={fmt_ms(totals.get('wall_time_s'))}/"
                    f"{fmt_ms(totals.get('gpu_busy_s'))}ms; top={top.get('name')} "
                    f"{fmt_ms(top.get('self_time_s'))}ms/{round(top.get('pct_of_wall') or 0,1)}% "
                    f"calls={top.get('calls')}; shape={','.join(shape['labels'])}.{launch_text}"
                    if ncu else
                    f"MEASURE: ROI={fmt_ms(totals.get('wall_time_s'))}ms{gpu_measure}; "
                    f"top={top.get('name')} {fmt_ms(top.get('self_time_s'))}ms/"
                    f"{round(top.get('pct_of_wall') or 0,1)}% calls={top.get('calls')}; "
                    f"shape={','.join(shape['labels'])}.{launch_text}")
    lines = [
        (0, header),
        (0, measure_line),
        (0, read_line),
        *(([(0, line) for line in ncu_lines] if ncu_lines else [])),
        (2 if ncu else 1, f"SOURCE FACTS: {code_facts}; syntax facts, not profiler mechanisms."),
        (0, frontier_line),
        (1 if ncu else 0, f"HISTORY: trials={len(iterations)} classes={len(history)} "
            f"overused={','.join(overused) if overused else 'none'}; best={best_text}."),
        (2, f"BASELINE: build={request.get('build_command') or request.get('build')}; compare absolute ROI and speedup against this exact fingerprint."),
        (0, verify_line),
        (2, "DETAILS stay on disk in agent-brief.json; request one named drill-down only if it can change the next action."),
    ]
    count_tokens, method = token_counter()
    effective_max_tokens = (DEFAULT_NCU_MAX_TOKENS
                            if ncu and max_tokens == DEFAULT_MAX_TOKENS else max_tokens)
    selected = []
    for priority in range(3):
        for item_priority, line in lines:
            if item_priority != priority:
                continue
            candidate = "\n".join(selected + [line]) + "\n"
            if count_tokens(candidate) <= effective_max_tokens or item_priority == 0:
                selected.append(line)
    text = "\n".join(selected) + "\n"
    tokens = count_tokens(text)
    if tokens > effective_max_tokens:
        raise ValueError(f"essential brief needs {tokens} tokens, cap is {effective_max_tokens}")
    machine["context"] = {"text_tokens": tokens, "max_tokens": effective_max_tokens,
                          "token_method": method, "text_bytes": len(text.encode()),
                          "omitted": ["raw profile", "full commands", "samples", "hashes",
                                      "full source", "unchanged prior-phase facts"]}
    return machine, text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--phase", choices=["auto", "optimize", "iterate", "repair", "final"], default="auto")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--text-output", type=Path)
    parser.add_argument("--print-brief", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    machine, text = build(run, args.phase, args.max_tokens)
    json_output = args.json_output or run / "agent-brief.json"
    text_output = args.text_output or run / "agent-brief.txt"
    json_output.write_text(json.dumps(machine, indent=2) + "\n")
    text_output.write_text(text)
    if args.print_json:
        print(json.dumps(machine, indent=2))
    elif args.print_brief:
        print(text, end="")
    else:
        print(json.dumps({"status": "ok", "brief": str(text_output),
                          "tokens": machine["context"]["text_tokens"],
                          "action": machine["action"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
