#!/usr/bin/env python3
"""Find and persist bounded source slices for data-lifecycle evidence."""

from __future__ import annotations

import re
from pathlib import Path

from source_scan import repo_relative, write_slice


def code_only(text: str) -> str:
    """Remove comments and literals before applying lexical source scans."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', " ", text)


def data_sites(symbol: str, files: list[Path], context: int = 5) -> list[dict]:
    """Return one bounded declaration, transfer, and writer site for a symbol."""
    leaf = re.split(r"\.|->", symbol)[-1]
    token = rf"(?:\b{re.escape(symbol)}\b|\b{re.escape(leaf)}\b)"
    transfer = re.compile(
        rf"\b(?:cuda|hip)(?:Memcpy(?:Async)?|Malloc(?:Async|Managed)?|"
        rf"Free(?:Async)?|Memset(?:Async)?)\b[^;]*{token}",
        re.I,
    )
    writer = re.compile(rf"{token}\s*(?:\[[^\]]+\])?\s*=(?!=)")
    declaration = re.compile(
        rf"^(?:(?:const|static|volatile|unsigned|signed|struct)\s+)*"
        rf"[A-Za-z_]\w*(?:::\w+)*(?:\s*[*&]\s*|\s+)+"
        rf"{re.escape(leaf)}\b\s*(?:\[[^;]*\])?\s*(?:=|;)"
    )

    candidates = []
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines):
            clean = code_only(line).strip()
            kind, score = None, 0
            if declaration.search(clean):
                kind, score = "declaration", 6 if path.suffix in {".h", ".hpp", ".cuh"} else 4
            if writer.search(clean):
                kind, score = "writer", max(score, 5)
                nearby = "\n".join(lines[max(0, index - context):index + context + 1])
                score += len(re.findall(r"\b(?:for|while|if)\s*\(", code_only(nearby)))
            if transfer.search(clean):
                kind = "transfer-or-allocation"
                operation_scores = ((r"Memcpy", 12), (r"Malloc", 10), (r"Memset", 9))
                score = max(score, next((value for name, value in operation_scores
                                         if re.search(name, clean, re.I)), 8))
            if not kind:
                continue
            start, end = max(0, index - context), min(len(lines), index + context + 1)
            candidates.append({
                "path": path, "match": index, "start": start, "end": end,
                "method": f"data-{kind}-window", "confidence": "medium",
                "candidates": 1, "lines": lines, "kind": kind, "score": score,
            })

    transfers = [item for item in candidates if item["kind"] == "transfer-or-allocation"]
    transfer_path = max(transfers, key=lambda item: (item["score"], -item["match"]))["path"] \
        if transfers else None
    selected, used_files = [], set()
    for kind in ("declaration", "transfer-or-allocation", "writer"):
        writer_path = transfer_path if kind == "writer" else None
        choices = sorted(
            (item for item in candidates if item["kind"] == kind),
            key=lambda item: (
                0 if writer_path and item["path"] == writer_path else 1,
                item["path"] in used_files, -item["score"], item["match"],
            ),
        )
        if choices:
            selected.append(choices[0])
            used_files.add(choices[0]["path"])
    return selected[:3]


def same_range(found: dict, target: dict, repo: Path) -> bool:
    """Return whether a resolved match is already inside an emitted target."""
    source = target.get("source") or {}
    path = Path(source.get("path", ""))
    path = path if path.is_absolute() else repo / path
    return (path.resolve() == found["path"].resolve()
            and source.get("start_line", 0) <= found["match"] + 1
            <= source.get("end_line", -1))


def append_target(found: dict, symbol: str, relation: str, parent: str,
                  targets: list[dict], slices: Path, repo: Path) -> dict:
    """Write one slice and append its compact source target metadata."""
    target_id = f"target-{len(targets) + 1:02d}"
    path = slices / f"{target_id}.txt"
    size, digest = write_slice(
        path, found["path"], found["lines"], found["start"], found["end"],
        f"{relation}: {symbol}",
    )
    target = {
        "id": target_id, "hotspot": symbol, "wall_pct": None, "symbol": symbol,
        "dependency": {"relation": relation, "parent": parent},
        "source": {
            "path": repo_relative(found["path"], repo),
            "start_line": found["start"] + 1,
            "end_line": found["end"],
            "match_line": found["match"] + 1,
        },
        "slice": {"path": repo_relative(path, repo), "bytes": size, "sha256_16": digest},
        "resolution": {
            "method": found["method"], "confidence": found["confidence"],
            "candidate_matches": found["candidates"],
        },
    }
    targets.append(target)
    return target


def data_priority(site: dict) -> int:
    """Rank lifecycle evidence using structure rather than symbol-name hints."""
    bonus = {"transfer-or-allocation": 4, "writer": 1}.get(site.get("kind"), 0)
    return int(site.get("score", 0)) + bonus


_MEMCPY_CALL = re.compile(
    r"\b(?:cuda|hip)Memcpy(?:Async)?\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,", re.I
)


def _strip_mem_arg(argument: str) -> str:
    argument = argument.strip()
    while True:
        changed = re.subn(r"^\([^()]*\)\s*", "", argument)[0]
        if changed == argument:
            break
        argument = changed
    return argument.lstrip("&*(").rstrip(") ").strip()


def memcpy_peer_symbol(line: str, symbol: str) -> str | None:
    """Resolve the other endpoint of a CUDA/HIP memory copy when possible."""
    match = _MEMCPY_CALL.search(line)
    if not match:
        return None
    left, right = _strip_mem_arg(match.group(1)), _strip_mem_arg(match.group(2))
    leaf = re.split(r"\.|->", symbol)[-1]
    if left and right and (symbol in left or leaf in left):
        return right
    if left and right and (symbol in right or leaf in right):
        return left
    return None
