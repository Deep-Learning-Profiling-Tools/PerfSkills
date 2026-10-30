#!/usr/bin/env python3
"""Render the bounded text view of an agent brief."""

from pathlib import Path

from brief_facts import fmt_ms, format_oracle, token_counter
from brief_ncu import render_ncu


def _trim(value: str | None, max_chars: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max(0, (max_chars - 3) // 2)
    tail = max(0, max_chars - 3 - head)
    return text[:head] + "..." + text[-tail:] if tail else text[:head] + "..."


def _compact_refs(source_refs: list[dict], limit: int) -> str:
    refs = []
    for item in source_refs or []:
        if not item.get("path"):
            continue
        start, end = (item.get("lines") or [None, None])[:2]
        if not start or not end:
            continue
        refs.append(f"{item['id']}={Path(item['path']).name}:{start}-{end}")
    if not refs:
        return "source mapping unresolved"
    if limit > 0 and len(refs) > limit:
        # Keep overflow addressable without spelling every path in the terminal view.
        # The machine brief/source-targets.json retains the full path and range.
        shown = ", ".join(refs[:limit])
        return f"{shown}; +{len(refs) - limit} targets"
    return ", ".join(refs)


def _compact_dependency_map(source_refs: list[dict]) -> str:
    """Summarize the full bounded frontier by relationship, without source text."""
    groups = {}
    aliases = {
        "existing-variant": "v", "callee": "c", "transfer-or-allocation": "t",
        "writer": "w", "hotspot": "h", "declaration": "d",
    }
    for item in source_refs or []:
        target_id = item.get("id")
        relation = item.get("relation") or "hotspot"
        if target_id:
            parent = item.get("parent")
            target_id = target_id.replace("target-", "")
            parent = str(parent).replace("target-", "") if parent else None
            groups.setdefault(aliases.get(relation, "x"), []).append(
                f"{target_id}>{parent}" if parent else target_id
            )
    if not groups:
        return "none"
    return ("v=variant,c=callee,t=transfer,w=writer,h=hotspot; " + ";".join(
        f"{relation}:{','.join(ids)}" for relation, ids in sorted(groups.items())
    ))


def _compact_latest(latest: list[dict], limit: int) -> str:
    if limit <= 0:
        return ""
    rows = [
        (f"{item['id']} ROI={round(item['roi_speedup'],3) if item['roi_speedup'] else None}x "
         f"{'PASS' if item['correct'] else 'FAIL'} "
         f"kernel={round(item['mechanism_kernel_speedup'],3) if item['mechanism_kernel_speedup'] else None}x")
        for item in (latest or [])
    ]
    if limit > 0 and len(rows) > limit:
        return "; ".join(rows[:limit]) + f"; +{len(rows) - limit} more"
    return "; ".join(rows)


def render(run, phase, action, request, fingerprint, totals, top, shape, source_refs,
           frontier, history, iterations, best, features, ncu, latest, failed_gates,
           variant_refs=None, max_tokens=320, default_tokens=320, ncu_tokens=480):
    launch = shape.get("top_launch") or {}
    launch_text = ""
    if launch:
        grid = "x".join(map(str, launch.get("grid") or []))
        block = "x".join(map(str, launch.get("block") or []))
        launch_text = f" grid={grid} block={block}"
    api_text = ""
    if shape.get("kernel_launches") is not None:
        sync = shape.get("synchronizations") or 0
        copies = shape.get("copy_calls") or 0
        copy_gb = (shape.get("copy_bytes") or 0) / 1e9
        sync_s = shape.get("synchronization_time_s") or 0
        api_text = f" app=L={shape['kernel_launches']} S={sync}/{sync_s:.3g}s C={copies}/{copy_gb:.3g}GB"
    families = []
    for item in (shape.get("hotspot_families") or [])[:3]:
        families.append(f"{item['name']}={item['time_s'] * 1000:.3g}ms/{item['calls']}")
    family_text = f" fam={','.join(families)}" if families else ""
    facts = ",".join((features or [])[:6]) or "none detected in emitted slices"
    oracle = _trim(format_oracle(request.get("correctness") or request.get("oracle") or {}), 120)
    best_text = (f"i{best['iteration']} {best['value']:.6g} {best.get('unit') or ''} "
                 f"{round(best['improvement'],3)}x"
                 if best and best.get("improvement") is not None else str(best))
    header = (f"ProfileWeaver {phase} run={run.name}; measured evidence -> one candidate."
              if ncu else f"ProfileWeaver {phase} run={run.name}; ACTION: {action}.")
    count, method = token_counter()
    cap = ncu_tokens if ncu and max_tokens == default_tokens else max_tokens
    dependency_map = _compact_dependency_map((source_refs or []) + (variant_refs or []))

    for level in range(6):
        targets = _compact_refs(source_refs, 4 if level <= 1 else 2 if level == 2 else 0)
        read_line = (f"READ ONLY: {targets}; dependency chain only."
                     if ncu else f"READ: {targets}; only these slices.")
        variants = _compact_refs(variant_refs or [], 1)
        if level >= 3:
            read_line = ("READ ONLY: source-targets.json/source-slices; dependency chain only."
                         if ncu else "READ: see source-targets.json and source-slices/; only those slices.")
        if variant_refs and not ncu:
            read_line = read_line.rstrip(".") + f"; variants={variants}."
        top_name = _trim(top.get("name"), 72 if level <= 1 else 64 if level == 2 else 52)
        setup = ""
        if shape.get("setup_copy_s"):
            setup = (f" setup-copy={shape.get('setup_copy_bytes', 0) / 1e6:.1f}MB/"
                     f"{fmt_ms(shape.get('setup_copy_s'))}ms")
        measure = (f"TIME: ROI/GPU={fmt_ms(totals.get('wall_time_s'))}/"
                   f"{fmt_ms(totals.get('gpu_busy_s'))}ms; top={top_name} "
                   f"{fmt_ms(top.get('self_time_s'))}ms/{round(top.get('pct_of_wall') or 0,1)}% "
                   f"calls={top.get('calls')}; shape={','.join(shape['labels'])}.{setup}{launch_text}"
                   f"{api_text}{family_text}")
        latest_text = _compact_latest(latest, 2 if level <= 1 else 1 if level == 2 else -1)
        include_latest = bool(latest_text) and level <= 3
        include_facts = level <= 1
        include_history = level <= 2
        ncu_lines = render_ncu(
            ncu,
            stall_limit=3 if level <= 1 else 2 if level == 2 else 1,
            rule_limit=3 if level <= 1 else 2 if level == 2 else 1,
            include_mem=(level <= 2),
            include_diag=(level <= 3),
        )
        verify = (f"GATE fp={(fingerprint.get('contract_sha256') or 'MISSING')[:12]} "
                  f"oracle={oracle}; pair=ROI+NCU; fail={failed_gates or 'none'}." )
        lines = [
            (0, header), (0, measure), (0, read_line),
            (0, "EVIDENCE: measured counters/slices; agent chooses edits; multi-file allowed."),
            (0, verify),
            (2, f"DEPENDENCY FRONTIER: {dependency_map}; connected slices are valid."),
            *(([(0, f"LAST RESULTS: {latest_text}; ROI decides, NCU kernel delta explains.")]
               if include_latest else [])),
            *(([(0, line) for line in ncu_lines] if ncu_lines else [])),
            *(([(2 if ncu else 1, f"SOURCE FACTS: {facts}; syntax facts, not profiler mechanisms.")]
               if include_facts else [])),
            *(([(3 if ncu else 0, f"HISTORY: trials={len(iterations)} classes={len(history)} "
                f"best={best_text}.")]
               if include_history else [])),
        ]
        selected = []
        for priority in range(3):
            for item_priority, line in lines:
                candidate = "\n".join(selected + [line]) + "\n"
                if item_priority == priority and count(candidate) <= cap:
                    selected.append(line)
        text = "\n".join(selected) + "\n"
        tokens = count(text)
        if tokens <= cap:
            break
    else:
        text = (f"ProfileWeaver {phase} run={run.name}; brief truncated to fit {cap} tokens.\n"
                f"See {run / 'agent-brief.json'} and {run / 'source-targets.json'}.\n")
        tokens = count(text)

    context = {"text_tokens": tokens, "max_tokens": cap, "token_method": method,
               "text_bytes": len(text.encode()),
               "omitted": ["raw profile", "full commands", "samples", "hashes", "full source"]}
    return text, context
