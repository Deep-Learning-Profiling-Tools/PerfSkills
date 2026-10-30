#!/usr/bin/env python3
"""Bounded C/C++/CUDA source symbol location primitives."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".cu", ".cuh", ".h", ".hh", ".hpp", ".hxx", ".f", ".f90", ".f95"}
IGNORED_PARTS = {".git", "build", "cmake-build-debug", "cmake-build-release", "external", "raw", "__pycache__"}


def load(path: Path):
    return json.loads(path.read_text())


def repo_relative(path: Path, repo: Path):
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path.resolve())


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
