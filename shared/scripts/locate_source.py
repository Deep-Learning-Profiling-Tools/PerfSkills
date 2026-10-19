#!/usr/bin/env python3
"""Resolve measured hotspots to small, auditable source ranges.

The profiler remains the authority for *which* operation matters.  This script only maps the
operation name to source and writes numbered slices; it never claims that text search proves a
performance cause.  Agents should read the emitted slices before searching the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".cu", ".cuh", ".h", ".hh", ".hpp", ".hxx", ".f", ".f90", ".f95"}
IGNORED_PARTS = {".git", "build", "cmake-build-debug", "cmake-build-release", "external", "raw", "__pycache__"}
LIBRARY_MARKERS = ("cub::", "cuda::", "std::", "CUDA memcpy", "CUDA memset")


def load(path: Path):
    return json.loads(path.read_text())


def repo_relative(path: Path, repo: Path):
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path.resolve())


def symbol_from_name(name: str):
    if any(marker in name for marker in LIBRARY_MARKERS):
        return None
    head = name.split("(", 1)[0].strip()
    head = re.sub(r"^(?:void|int|float|double|long|unsigned|static|__global__|__device__)\s+", "", head)
    token = re.split(r"\s+", head)[-1]
    token = token.rsplit("::", 1)[-1]
    token = re.sub(r"<.*$", "", token)
    return token if re.match(r"^[A-Za-z_]\w*$", token) else None


def source_files(roots):
    seen = set()
    for root in roots:
        root = root.resolve()
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            rel_parts = path.relative_to(root).parts if root.is_dir() else ()
            if any(part in IGNORED_PARTS for part in rel_parts):
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield resolved


def function_range(lines, match_index, max_lines, context):
    """Return a bounded one-based range enclosing the matched function when braces are visible."""
    open_line = None
    depth = 0
    for idx in range(match_index, min(len(lines), match_index + 12)):
        if "{" in lines[idx]:
            open_line = idx
            break
        if ";" in lines[idx]:
            break
    if open_line is None:
        return max(0, match_index - context), min(len(lines), match_index + context + 1), "symbol-window"
    end = None
    for idx in range(open_line, len(lines)):
        # This is deliberately lexical, not a C++ parser. It is only a bounded reading aid.
        depth += lines[idx].count("{") - lines[idx].count("}")
        if depth == 0:
            end = idx + 1
            break
    if end is None:
        end = min(len(lines), open_line + max_lines)
    start = max(0, match_index - context)
    end = min(len(lines), end + context)
    if end - start > max_lines:
        end = start + max_lines
        method = "function-prefix-truncated"
    else:
        method = "function-body"
    return start, end, method


def match_score(line, symbol):
    if not re.search(rf"\b{re.escape(symbol)}\b", line):
        return -1
    score = 1
    if "<<<" in line:
        score -= 1
    if re.search(rf"\b{re.escape(symbol)}\s*\(", line):
        score += 2
    if any(word in line for word in ("__global__", "__device__", "void ", "int ", "double ", "float ", "auto ")):
        score += 2
    if line.lstrip().startswith(("//", "/*", "*")):
        score -= 2
    return score


def locate_symbol(symbol, files, max_lines, context):
    matches = []
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            score = match_score(line, symbol)
            if score >= 0:
                start, end, method = function_range(lines, idx, max_lines, context)
                if method.startswith("function"):
                    score += 3
                matches.append((score, -len(lines), path, idx, start, end, method, lines))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    score, _, path, idx, start, end, method, lines = matches[0]
    confidence = "high" if score >= 5 and method.startswith("function") else "medium" if score >= 3 else "low"
    return {"path": path, "match": idx, "start": start, "end": end, "method": method,
            "confidence": confidence, "candidates": len(matches), "lines": lines}


def locate_call_site(symbol, files, context):
    """Resolve a named call without returning its declaration or definition."""
    matches = []
    pattern = re.compile(rf"\b{re.escape(symbol)}\s*\(")
    declaration_words = ("__global__", "__device__", "__host__", "void ", "int ", "double ",
                         "float ", "long ", "unsigned ", "auto ")
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            if not pattern.search(line) or line.lstrip().startswith(("//", "/*", "*")):
                continue
            prefix = line[:pattern.search(line).start()]
            if any(word in prefix for word in declaration_words):
                continue
            start, end = max(0, idx - context), min(len(lines), idx + context + 1)
            score = 2 + (2 if "=" in prefix or "return" in prefix else 0) + (1 if ";" in line else 0)
            matches.append((score, path, idx, start, end, lines))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], str(item[1]), -item[2]), reverse=True)
    score, path, idx, start, end, lines = matches[0]
    confidence = "high" if len(matches) == 1 and score >= 4 else "medium" if score >= 2 else "low"
    return {"path": path, "match": idx, "start": start, "end": end,
            "method": "named-call-site-window", "confidence": confidence,
            "candidates": len(matches), "lines": lines}


def direct_location(item, repo, roots, max_lines, context):
    file_name, line = item.get("file"), item.get("line")
    if not file_name or not line:
        return None
    path = Path(file_name)
    if not path.is_absolute():
        path = repo / path
    path = path.resolve()
    allowed = any(path == root if root.is_file() else path.is_relative_to(root) for root in roots)
    if not allowed or not path.is_file():
        return None
    lines = path.read_text(errors="replace").splitlines()
    idx = max(0, int(line) - 1)
    start, end, method = function_range(lines, idx, max_lines, context)
    return {"path": path.resolve(), "match": idx, "start": start, "end": end,
            "method": "profiler-line+" + method, "confidence": "high", "candidates": 1, "lines": lines}


def write_slice(output_path, source_path, lines, start, end, label):
    body = [f"# {label}", f"# source: {source_path}:{start + 1}-{end}"]
    body.extend(f"{i + 1:6d} | {lines[i]}" for i in range(start, end))
    text = "\n".join(body) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text)
    return len(text.encode()), hashlib.sha256(text.encode()).hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path,
                        help="normalized profile; optional when one or more --symbol values are supplied")
    parser.add_argument("--symbol", action="append", default=[],
                        help="resolve one explicitly named dependency instead of profile hotspots")
    parser.add_argument("--call-site", action="append", default=[],
                        help="resolve a bounded window around a named caller instead of its definition")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-root", action="append", type=Path, required=True,
                        help="repeatable; search is confined to these roots")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--append", action="store_true",
                        help="append named dependencies to an existing source-targets.json")
    parser.add_argument("--max-targets", type=int, default=2)
    parser.add_argument("--max-lines", type=int, default=160)
    parser.add_argument("--context", type=int, default=4)
    args = parser.parse_args()

    repo, run = args.repo_root.resolve(), args.run_dir.resolve()
    search_roots = [root.resolve() for root in args.source_root]
    if not args.profile and not args.symbol and not args.call_site:
        parser.error("supply --profile, --symbol, --call-site, or a combination")
    profile = load(args.profile.resolve()) if args.profile else {"hotspots": []}
    files = list(source_files(search_roots))
    output = args.output.resolve() if args.output else run / "source-targets.json"
    slices_dir = run / "source-slices"
    if not args.append and slices_dir.exists():
        for stale in slices_dir.glob("target-*.txt"):
            stale.unlink()
    prior = load(output) if args.append and output.exists() else {}
    targets, unresolved = list(prior.get("targets") or []), list(prior.get("unresolved") or [])
    original_target_count = len(targets)
    explicit = ([{"name": symbol, "kind": "unknown", "pct_of_wall": None,
                  "resolution_mode": "definition"} for symbol in args.symbol] +
                [{"name": symbol, "kind": "unknown", "pct_of_wall": None,
                  "resolution_mode": "call-site"} for symbol in args.call_site])
    actionable = (explicit if explicit else
                  [h for h in profile.get("hotspots", []) if h.get("kind") not in {"memcpy", "sync", "runtime", "library"}])

    for hotspot in actionable:
        if len(targets) - original_target_count >= args.max_targets:
            break
        explicit_mode = hotspot.get("resolution_mode")
        symbol = hotspot.get("name") if explicit_mode else symbol_from_name(hotspot.get("name", ""))
        found = None if explicit_mode else direct_location(hotspot, repo, search_roots, args.max_lines, args.context)
        if found is None and symbol and explicit_mode == "call-site":
            found = locate_call_site(symbol, files, args.context)
        elif found is None and symbol:
            found = locate_symbol(symbol, files, args.max_lines, args.context)
        if found is None:
            unresolved.append({"hotspot": hotspot.get("name"), "symbol": symbol})
            continue
        target_id = f"target-{len(targets) + 1:02d}"
        slice_path = slices_dir / f"{target_id}.txt"
        byte_count, digest = write_slice(slice_path, found["path"], found["lines"], found["start"], found["end"], hotspot.get("name"))
        targets.append({
            "id": target_id,
            "hotspot": hotspot.get("name"),
            "wall_pct": hotspot.get("pct_of_wall"),
            "symbol": symbol,
            "source": {
                "path": repo_relative(found["path"], repo),
                "start_line": found["start"] + 1,
                "end_line": found["end"],
                "match_line": found["match"] + 1,
            },
            "slice": {"path": repo_relative(slice_path, repo), "bytes": byte_count, "sha256_16": digest},
            "resolution": {"method": found["method"], "confidence": found["confidence"],
                           "candidate_matches": found["candidates"]},
        })

    record = {
        "protocol": "profileweaver.source-targets/v1",
        "profile": (repo_relative(args.profile.resolve(), repo) if args.profile else prior.get("profile")),
        "search_roots": sorted(set((prior.get("search_roots") or []) + [repo_relative(p, repo) for p in search_roots])),
        "policy": "Read emitted slices first. Expand a range only for a named dependency required by the proposed change.",
        "targets": targets,
        "unresolved": unresolved[:5],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"status": "ok" if targets else "unresolved", "targets": len(targets),
                      "unresolved": len(unresolved), "output": repo_relative(output, repo)}, separators=(",", ":")))
    return 0 if targets else 2


if __name__ == "__main__":
    sys.exit(main())
