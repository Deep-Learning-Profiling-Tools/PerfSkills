#!/usr/bin/env python3
"""Extract bounded timeline, source, history, and NCU facts for an agent brief."""
from __future__ import annotations
import json
import math
import re
from pathlib import Path
from experiment_support import REPO
from kernel_evidence import compact_ncu
from timeline_summary import summarize
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
    declared = str(item.get("mechanism") or "").lower()
    text = (declared + " " + str(item.get("optimization", ""))).lower()
    matches = []
    rules = [
        ("compact-guide-or-bin-index", r"compact.guide|bin.index|sparse.guide|bounded.interval|binning"),
        ("lookup-reuse-or-grouping", r"lookup.reuse|query.reuse|group(?:ed|ing).*(?:lookup|query)|reuse.*(?:search|index)"),
        ("indirect-access-layout", r"indirect.*(?:layout|access)|coalesc|structure.of.arrays|array.of.structures"),
        ("dependent-lookup-elimination", r"dependent.*(?:lookup|address)|amortiz.*(?:lookup|address)"),
        ("invariant-or-query-reuse", r"invariant.*(?:reuse|hoist)|query.*intermediate|reuse.*intermediate"),
        ("reuse-after-ordering", r"reuse.*(?:order|sort)|ordered.*(?:reuse|search)"),
        ("reuse-existing-variant", r"existing.*(?:variant|sibling)|adapt.*variant"),
        ("resident-data-or-index-compression", r"resident|compress.*(?:index|data)|index.*compress"),
        ("search-or-index-structure", r"directory|bounded|index|bin(?:s|ning)?|binary.search|lower.bound"),
        # Keep this classifier domain-neutral.  Field names such as "material" or
        # "energy" belong to a workload's source and must never become a framework
        # hypothesis or mechanism label.
        ("data-layout-or-ordering", r"sort|radix|key.pack|key.order|work.order|query.order|layout|reorder"),
        ("coalesced-access", r"coalesc|vectori[sz]|align|pack"),
        ("algorithmic-work-reduction", r"precomput|reciprocal|invariant|hoist|work.reduction"),
        ("work-grouping", r"group|partition|specializ|fast.path"),
        ("launch-resource-balance", r"threads.per.block|block.size|launch.bounds|parameter|tuning"),
        ("traffic-elimination", r"alloc|free|resident|reuse.buffer|traffic"),
        ("fusion", r"fus|combined kernel|single kernel"),
        ("instruction-mix", r"arithmetic|instruction|math|precision"),
    ]
    for label, pattern in rules:
        if re.search(pattern, text):
            matches.append(label)
    return matches or ([declared] if declared else ["other"])


def source_features(run: Path, targets):
    text = ""
    used = []
    has_variant = False
    for target in targets:
        if (target.get("dependency") or {}).get("relation") == "existing-variant":
            has_variant = True
            # A sibling is a search-space hint, not baseline evidence.
            continue
        path = resolve_artifact(run, (target.get("slice") or {}).get("path"))
        if path:
            text += "\n" + path.read_text(errors="replace")
            used.append(target.get("id"))
    checks = [
        ("exact-search", r"\b(?:binary_search|lower_bound|upper_bound|equal_range|grid_search|bsearch|probe_search)\b|\b(?:mid|middle)\s*=.*(?:low|left).*(?:high|right)"),
        ("state-transition", r"\b(?:rng|random|xorshift|philox|curand)[A-Za-z0-9_]*\b"),
        ("accumulation-or-reduction", r"(?:\+=|\*=|atomicAdd|\breduce(?:_if)?\s*\()"),
        ("indirect-memory-access", r"\[[^\]\n]*(?:\*|\+|->|\b(?:idx|index|offset|stride|row|col|bin|bucket|nuc|mat)\b)[^\]\n]*\]"),
        ("allocation-in-target", r"\b(?:malloc|calloc|realloc|new|cudaMalloc|hipMalloc|omp_target_alloc)\s*[<(]"),
        ("explicit-synchronization", r"\b(?:cuda|hip)(?:Device|Stream)Synchronize\s*\(|\b(?:barrier|taskwait)\b"),
        ("host-device-transfer", r"\b(?:cuda|hip)Memcpy(?:Async)?\s*\(|\bomp_target_memcpy\s*\("),
        ("atomic-update", r"\b(?:atomic[A-Za-z0-9_]*|#pragma\s+omp\s+atomic)\b"),
        ("sort-or-partition", r"\b(?:sort|stable_sort|partition|DeviceRadixSort|sort_by_key)\s*(?:::|\()"),
    ]
    facts = [name for name, pattern in checks if re.search(pattern, text, re.S)]
    loop_count = len(re.findall(r"\b(?:for|while)\s*\(", text))
    if loop_count >= 2:
        facts.append("nested-or-repeated-loop")
    if len(re.findall(r"\b(?:if|switch)\s*\(", text)) >= 3:
        facts.append("branch-dense-target")
    if has_variant:
        facts.append("existing-variant-implementation")
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
    summary = summarize(profile or {}, details or {})
    launch_count = summary.get("kernel_launches")
    copy = gpu.get("copy_union_s") or 0
    scope = details.get("scope") or {}
    pre_copy = ((scope.get("copies") or {}).get("before_roi") or {})
    pre_copy_s = pre_copy.get("union_s") or 0
    pre_copy_bytes = pre_copy.get("bytes") or 0
    labels = []
    if top_share >= 60:
        labels.append("dominant-long-kernel")
    if wall and isinstance(busy, (int, float)) and busy / wall < 0.5:
        labels.append("gpu-gap-heavy")
    if wall and copy / wall >= 0.2:
        labels.append("copy-heavy")
    if wall and pre_copy_s / wall >= 0.2:
        labels.append("setup-copy-heavy")
    if launch_count is not None and launch_count >= 100 and gpu.get("kernel_duration_s", {}).get("median", 0) < 50e-6:
        labels.append("fragmented-small-kernels")
    if not labels:
        labels.append("mixed-timeline")
    return {
        "labels": labels,
        "gpu_busy_pct": round(busy / wall * 100, 2) if wall and isinstance(busy, (int, float)) else None,
        "copy_pct": round(copy / wall * 100, 2) if wall else None,
        "setup_copy_s": pre_copy_s,
        "setup_copy_bytes": pre_copy_bytes,
        "setup_copy_pct_of_roi": round(pre_copy_s / wall * 100, 2) if wall else None,
        "kernel_launches": launch_count,
        "top_launch": (details.get("top_launches") or [None])[0],
        "synchronizations": summary.get("synchronizations"),
        "synchronization_time_s": summary.get("synchronization_time_s"),
        "copy_calls": summary.get("copy_calls"),
        "copy_time_s": summary.get("copy_time_s"),
        "copy_bytes": summary.get("copy_bytes"),
        "hotspot_families": summary.get("hotspot_families") or [],
    }


def ncu_capsule(run: Path, profile, source_facts=None):
    """Return only decision-bearing NCU facts; never copy the full counter table."""
    diagnostics = load(run / "kernel-diagnostics.json", {})
    if not diagnostics:
        diagnostics = (profile or {}).get("kernel_diagnostics") or {}
    merged = dict(profile or {})
    merged["kernel_diagnostics"] = diagnostics
    return compact_ncu(merged, source_features=source_facts)


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
