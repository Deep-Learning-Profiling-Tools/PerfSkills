"""Literal source-control observations, not recipes or measured execution paths."""

from collections import Counter
from pathlib import Path
import re

from experiment_support import load, write

DISABLED = re.compile(r"\bif\s*\(\s*(?:false|0)\s*\)|^\s*#\s*if\s+0\b", re.M)


def clean(text):
    pattern = r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m[0]), text, flags=re.S)


def controls(plan, run, state, candidate):
    names, seen, current = set(), set(), candidate
    while current != "baseline" and current not in seen:
        seen.add(current)
        patch = run / "candidates" / current / "change.patch"
        if patch.is_file():
            names.update(line.removeprefix("+++ candidate/") for line in patch.read_text().splitlines()
                         if line.startswith("+++ candidate/"))
        current = state.get("candidates", {}).get(current, {}).get("parent", "baseline")
    workspace = Path(state["candidates"][candidate]["workspace"])
    baseline = Path(plan["source"]["baseline_dir"])
    facts, skipped = [], []
    for name in sorted(names):
        source, old = workspace / name, baseline / name
        if not source.resolve().is_relative_to(workspace.resolve()) or not source.is_file():
            continue
        if source.stat().st_size > 512000:
            skipped.append(name)
            continue
        before = clean(old.read_text(errors="replace")) if old.is_file() else ""
        after = clean(source.read_text(errors="replace"))
        original = Counter(re.sub(r"\s+", "", m[0]) for m in DISABLED.finditer(before))
        for m in DISABLED.finditer(after):
            key = re.sub(r"\s+", "", m[0])
            if original[key]:
                original[key] -= 1
            else:
                facts.append({"file": name, "line": after.count("\n", 0, m.start()) + 1,
                              "observation": "literal-disabled-conditional", "condition": key})
    output = {"snapshot": candidate, "comparison": "declared-baseline-source", "controls": facts,
              "skipped_large_sources": skipped, "scope": "lexical-not-execution-or-performance-evidence"}
    write(run / "candidates" / candidate / "source-observations.json", output)
    return output
