#!/usr/bin/env python3
"""Find bounded sibling implementations of a measured source symbol.

Existing variants are useful search-space evidence: they let an agent test a measured
algorithmic alternative instead of inventing a local knob. This module only scans the
declared source roots and returns small function slices; it never treats a sibling as a
measurement or as proof that the implementation is correct. A sibling may be in the
same file or another declared source file, which keeps producer/consumer and
initialization alternatives visible without exposing the whole tree.
"""

from __future__ import annotations

import re
from pathlib import Path

from source_scan import locate_symbol, repo_relative, write_slice


_SUFFIX = re.compile(r"_(?:baseline|original|reference|naive|optimized?)$", re.I)
_NUMBERED = re.compile(r"_(?:optimization|variant|version|impl)_\d+$", re.I)
_DECL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_KEYWORDS = re.compile(
    r"\b(?:sort|partition|index|guide|bin|search|lookup|reuse|resident|layout|"
    r"coalesc|shared|constant|texture|prefetch|fuse|compress)\w*\b", re.I)
_DECLARATION_WORDS = (
    "__global__", "__device__", "__host__", "static", "inline", "extern", "void ",
    "int ", "long ", "double ", "float ", "bool ", "auto ", "struct ",
)
_CALL_ONLY = {"if", "for", "while", "switch", "catch", "sizeof", "return", "min", "max"}


def _family(symbol: str) -> str:
    """Return the common stem for baseline/numbered sibling names."""
    value = _SUFFIX.sub("", symbol)
    value = _NUMBERED.sub("", value)
    return value


def _candidate_names(lines: list[str], stem: str, baseline: str) -> list[str]:
    names = []
    patterns = [re.compile(rf"\b{re.escape(stem)}_[A-Za-z_]\w*\s*\(")]
    # A measured kernel is often launched by a family of alternate driver functions.
    # When the baseline naming convention is explicit, expose those same-file drivers too;
    # this is how a pre-existing algorithmic path becomes visible without a whole-file read.
    if _SUFFIX.search(baseline):
        patterns.append(re.compile(r"\b[A-Za-z_]\w*_(?:optimization|variant|version|impl)_\d+\s*\("))
    for line in lines:
        for pattern in patterns:
            for match in pattern.finditer(line):
                name = _DECL.search(line[match.start():])
                if not name:
                    continue
                value = name.group(1)
                if value == baseline or value in names:
                    continue
                # A call site has no declaration keyword.  The lexical test is intentionally
                # permissive because CUDA declarations often span two lines.
                prefix = line[:match.start()]
                if not any(token in prefix for token in
                           ("__global__", "__device__", "__host__", "void ", "int ",
                            "long ", "double ", "float ", "auto ")):
                    continue
                names.append(value)
    return names


def _declared_names(lines: list[str], baseline: str) -> list[str]:
    """Collect likely definitions without parsing a whole translation unit."""
    names = []
    for index, line in enumerate(lines):
        for match in _DECL.finditer(line):
            name = match.group(1)
            if name == baseline or name in names or name in _CALL_ONLY:
                continue
            prefix = line[:match.start()]
            following = " ".join(lines[index:min(len(lines), index + 3)])
            # Require a declaration-like prefix or a body opening nearby. This avoids
            # turning ordinary calls in a neighboring function into variants.
            if not (any(word in prefix for word in _DECLARATION_WORDS) or "{" in following):
                continue
            if line.lstrip().startswith(("//", "/*", "*")):
                continue
            names.append(name)
    return names


def _score(found: dict, symbol: str, baseline_terms: set[str] | None = None) -> tuple[int, int, str]:
    body = "\n".join(found["lines"][found["start"]:found["end"]])
    # Prefer implementations that expose a structural mechanism, then retain a stable
    # name order.  This is a discovery ranking, never a predicted performance ranking.
    words = {item.lower() for item in _KEYWORDS.findall(body)}
    overlap = len(words & (baseline_terms or set()))
    return (len(words) + overlap * 2, found["end"] - found["start"], symbol)


def discover(symbol: str, hotspot_path: Path, files: list[Path], max_lines: int,
             context: int, maximum: int = 3) -> list[dict]:
    """Return at most ``maximum`` bounded alternatives in the declared source tree.

    Named same-file variants are preferred when present, but room is reserved for a
    structurally related definition in another source file. The ranking is evidence
    context only; it is never a predicted performance ranking.
    """
    if not symbol or maximum <= 0 or not hotspot_path or not hotspot_path.is_file():
        return []
    stem = _family(symbol)
    if not stem:
        return []
    try:
        lines = hotspot_path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    baseline_terms = {item.lower() for item in _KEYWORDS.findall("\n".join(lines))}
    names = _candidate_names(lines, stem, symbol)
    for path in files:
        if path.resolve() == hotspot_path.resolve():
            continue
        try:
            sibling_lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for name in _declared_names(sibling_lines, symbol):
            if name not in names:
                names.append(name)
    found = []
    seen = set()
    for name in names:
        item = locate_symbol(name, files, min(max_lines, 180), context)
        if not item:
            continue
        key = (name, item["path"].resolve())
        if key in seen or (item["path"].resolve() == hotspot_path.resolve() and name == symbol):
            continue
        seen.add(key)
        found.append((_score(item, name, baseline_terms), name, item))
    found.sort(key=lambda row: row[0], reverse=True)
    selected = []
    same = [row for row in found if row[2]["path"].resolve() == hotspot_path.resolve()]
    sibling = [row for row in found if row[2]["path"].resolve() != hotspot_path.resolve()]
    # Preserve a same-file candidate and at least one sibling-file candidate when both
    # exist; this is the compact representation frontier the agent was missing.
    if same:
        selected.append(same[0])
    if sibling and len(selected) < maximum:
        selected.append(sibling[0])
    if len(selected) < maximum:
        for row in found:
            if row not in selected:
                selected.append(row)
                if len(selected) >= maximum:
                    break
    return [{"name": name, "found": item, "family": stem,
             "scope": "same-file" if item["path"].resolve() == hotspot_path.resolve()
                      else "sibling-source-tree"}
            for _, name, item in selected]


def append_variant(item: dict, target: dict, target_id: str, slices: Path,
                   repo: Path) -> dict:
    """Write one sibling slice and return its source-target record."""
    found = item["found"]
    path = slices / f"{target_id}.txt"
    size, digest = write_slice(path, found["path"], found["lines"], found["start"],
                               found["end"], f"existing variant: {item['name']}")
    try:
        source_path = str(found["path"].resolve().relative_to(repo.resolve()))
    except ValueError:
        source_path = str(found["path"].resolve())
    return {
        "id": target_id, "hotspot": target.get("hotspot"), "wall_pct": None,
        "symbol": item["name"], "dependency": {
            "relation": "existing-variant", "parent": target["id"],
            "family": item["family"],
        },
        "source": {"path": source_path, "start_line": found["start"] + 1,
                   "end_line": found["end"], "match_line": found["match"] + 1},
        "slice": {"path": repo_relative(path, repo),
                  "bytes": size, "sha256_16": digest},
        "resolution": {"method": "existing-sibling-function", "scope": item.get("scope", "same-file"),
                        "confidence": "medium",
                        "candidate_matches": found["candidates"]},
    }
