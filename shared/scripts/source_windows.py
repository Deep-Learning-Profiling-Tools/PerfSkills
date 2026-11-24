"""Bounded source windows and emitted slices."""

import hashlib
from pathlib import Path


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

