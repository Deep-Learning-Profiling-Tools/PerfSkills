#!/usr/bin/env python3
"""Bounded C/C++/CUDA source symbol location primitives."""

from __future__ import annotations

from source_windows import function_range, direct_location, write_slice

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


def matching_backend(files, profile):
    """For CUDA evidence, prefer CUDA files over equivalent HIP/SYCL siblings."""
    tool = (profile.get("tool") or {}).get("name")
    if tool not in {"cupti-activity", "nsight-systems", "nsight-compute"}:
        return files
    known = set(files)
    return [path for path in files if not any(
        Path(*path.parts[:index], "cuda", *path.parts[index+1:]) in known
        for index, part in enumerate(path.parts[:-1]) if part.lower() in {"hip", "sycl"})]


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


def locate_symbols(symbols, files, max_lines, context):
    """Locate several symbols in one source pass.

    Variant discovery often asks for a bounded set of sibling names.  Calling
    ``locate_symbol`` separately for each name rereads every source file for
    every name, which becomes unbounded for large vendor translation units.
    Keep the same lexical scoring and result shape while indexing identifier
    tokens during a single pass over each file.
    """
    wanted = {symbol for symbol in symbols if symbol}
    matches = {symbol: [] for symbol in wanted}
    if not wanted:
        return {}
    for path in files:
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines):
            present = wanted.intersection(re.findall(r"\b[A-Za-z_]\w*\b", line))
            for symbol in present:
                score = match_score(line, symbol)
                if score < 0:
                    continue
                start, end, method = function_range(lines, idx, max_lines, context)
                if method.startswith("function"):
                    score += 3
                matches[symbol].append((score, -len(lines), path, idx, start, end,
                                        method, lines))
    result = {}
    for symbol, candidates in matches.items():
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        score, _, path, idx, start, end, method, lines = candidates[0]
        confidence = "high" if score >= 5 and method.startswith("function") else \
            "medium" if score >= 3 else "low"
        result[symbol] = {"path": path, "match": idx, "start": start, "end": end,
                          "method": method, "confidence": confidence,
                          "candidates": len(candidates), "lines": lines}
    return result


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


