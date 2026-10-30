#!/usr/bin/env python3
"""Emit a bounded hotspot, callee, and data-lifecycle source neighborhood."""

from __future__ import annotations

import re
from pathlib import Path

from source_dataflow import (
    append_target,
    code_only,
    data_priority,
    data_sites,
    memcpy_peer_symbol,
    same_range,
)
from source_scan import locate_symbol

IGNORED_CALLS = {
    "if", "for", "while", "switch", "return", "sizeof", "min", "max", "printf",
    "fprintf", "malloc", "calloc", "free", "cudaMalloc", "cudaFree", "cudaMemcpy",
    "atomicAdd", "atomicCAS", "__syncthreads", "__syncwarp", "__double_as_longlong",
    "__longlong_as_double", "__float_as_uint", "__uint_as_float", "__ldg",
}


def _symbols(lines: list[str], self_symbol: str | None = None) -> tuple[list[str], list[str]]:
    """Collect a small lexical call and data-reference frontier."""
    text = code_only("\n".join(lines))
    calls = []
    for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", text):
        if name not in IGNORED_CALLS and name != self_symbol and name not in calls:
            calls.append(name)
    indexed = []
    member = r"\b([A-Za-z_]\w*(?:(?:\.|->)[A-Za-z_]\w*)*)\s*\["
    for name in re.findall(member, text):
        if name not in indexed:
            indexed.append(name)
    for name in re.findall(r"\b([A-Za-z_]\w*(?:(?:\.|->)[A-Za-z_]\w+)+)", text):
        if name not in indexed:
            indexed.append(name)
    return calls[:12], indexed[:12]


def _callee_rank(found: dict) -> tuple[int, int, int]:
    body = code_only("\n".join(found["lines"][found["start"]:found["end"]]))
    control_flow = len(re.findall(r"\b(?:for|while|if)\s*\(", body))
    return body.count("["), control_flow, found["end"] - found["start"]


def _ranked_member_sites(indexed: list[str], body: list[str], files: list[Path],
                         context: int) -> list[tuple]:
    """Rank referenced data by read pressure and lifecycle evidence."""
    body_text = code_only("\n".join(body))
    ranked = []
    for symbol in (name for name in indexed if "." in name or "->" in name):
        sites = data_sites(symbol, files, context)
        preferred = next(
            (site for site in sites if site["kind"] == "transfer-or-allocation"),
            sites[0] if sites else None,
        )
        if not preferred:
            continue
        uses = body_text.count(symbol)
        writes = len(re.findall(
            rf"{re.escape(symbol)}\s*(?:\[[^\]]+\])?\s*(?:\+?=|=)(?!=)", body_text
        ))
        rank = (max(0, uses - writes), data_priority(preferred), -writes,
                int(preferred.get("match", 0)))
        ranked.append((rank, symbol, preferred))
    return sorted(ranked, reverse=True)


def _append_ranked_data(ranked: list[tuple], parent: dict, targets: list[dict],
                        slices: Path, repo: Path, files: list[Path], context: int,
                        seen: set, budget: int) -> int:
    """Append transfer sites first, then their producer when the copy exposes one."""
    transfers = [item for item in ranked if item[2].get("kind") == "transfer-or-allocation"]
    others = [item for item in ranked if item[2].get("kind") != "transfer-or-allocation"]
    selected = transfers[:3]
    selected += others[:max(0, 2 - len(selected))]
    added = 0
    for _, symbol, found in selected:
        if added >= budget or same_range(found, parent, repo):
            continue
        target = append_target(
            found, symbol, found["kind"], parent["id"], targets, slices, repo
        )
        seen.add(symbol)
        added += 1
        if added >= budget or found.get("kind") != "transfer-or-allocation":
            continue
        line = (found.get("lines") or [""])[found.get("match", 0)]
        peer = memcpy_peer_symbol(code_only(line), symbol)
        if not peer or peer in seen:
            continue
        peer_sites = data_sites(peer, files, context)
        writer = next((site for site in peer_sites if site.get("kind") == "writer"), None)
        if writer and not same_range(writer, target, repo):
            append_target(writer, peer, "writer", target["id"], targets, slices, repo)
            seen.add(peer)
            added += 1
    return added


def append_dependencies(targets: list[dict], files: list[Path], slices: Path, repo: Path,
                        depth: int, maximum: int, max_lines: int, context: int) -> None:
    """Add the highest-value dependency slices without broad source browsing."""
    if depth <= 0 or maximum <= 0:
        return
    queue = [(target, 0) for target in list(targets)]
    seen = {target.get("symbol") for target in targets}
    added = 0
    while queue and added < maximum:
        parent, level = queue.pop(0)
        if level >= depth:
            continue
        source = parent.get("source") or {}
        path = Path(source.get("path", ""))
        path = path if path.is_absolute() else repo / path
        if not path.is_file():
            continue
        lines = path.read_text(errors="replace").splitlines()
        start = max(0, source.get("match_line", source.get("start_line", 1)) - 1)
        end = max(start + 1, source.get("end_line", len(lines)) - context)
        body = lines[start:end]
        calls, indexed = _symbols(body, parent.get("symbol"))

        children = []
        if level < depth - 1:
            resolved = []
            for symbol in calls:
                if symbol in seen:
                    continue
                found = locate_symbol(symbol, files, min(max_lines, 120), context)
                if found and not same_range(found, parent, repo):
                    resolved.append((_callee_rank(found), symbol, found))
            for _, symbol, found in sorted(resolved, reverse=True)[:2 if level == 0 else 1]:
                if added >= maximum:
                    break
                child = append_target(found, symbol, "callee", parent["id"], targets,
                                      slices, repo)
                seen.add(symbol)
                added += 1
                children.append((child, level + 1))

        if children:
            ranked = _ranked_member_sites(indexed, body, files, context)
            added += _append_ranked_data(
                ranked, parent, targets, slices, repo, files, context, seen, maximum - added
            )
            queue = children + queue
            continue

        for symbol in indexed:
            if symbol in seen or added >= maximum:
                continue
            for found in data_sites(symbol, files, context):
                if added >= maximum or same_range(found, parent, repo):
                    continue
                child = append_target(found, symbol, found["kind"], parent["id"],
                                      targets, slices, repo)
                added += 1
                queue.append((child, level + 1))
            seen.add(symbol)
