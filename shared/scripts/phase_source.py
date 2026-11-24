"""Expose source evidence for an explicitly requested measured phase."""

import re
from pathlib import Path

from brief_facts import token_counter
from experiment_support import REPO, load
from phase_opportunity import current
from phase_trace import predecessors
from source_inspect import index_path
from source_scan import locate_call_site, matching_backend, source_files


def _match(paths, pattern):
    for path in paths:
        lines = path.read_text(errors="replace").splitlines()
        for index, line in enumerate(lines):
            if not line.lstrip().startswith(("//", "/*", "*")) and pattern.search(line):
                yield path, lines, index


def _window(path, lines, index, relation):
    low, high = max(0, index - 20), min(len(lines), index + 13)
    count, _ = token_counter()
    while high - low > 1 and count("\n".join(lines[low:high])) > 850:
        if index - low > high - index - 1:
            low += 1
        else:
            high -= 1
    return {"file": str(path), "line": index + 1, "start_line": low + 1,
            "relation": relation, "text": "\n".join(lines[low:high])}


def inspect(run, plan, cid):
    state = load(run / "experiment-state.json", {})
    cid = cid or "baseline"
    opportunity = current(run, plan, state, cid)
    result = {"snapshot": cid, "opportunity": opportunity}
    kind = opportunity.get("kind")
    host = opportunity.get("host_runtime") or (opportunity if kind == "repeated-operations" else None)
    if not host and kind not in {"application-phase", "kernel-family"}:
        return result
    snapshot = state.get("candidates", {}).get(cid) or {}
    workspace = Path(snapshot.get("workspace") or run / "baseline-source").resolve()
    if not workspace.is_dir() or not workspace.is_relative_to(run.resolve()):
        return result | {"source_status": "workspace-unavailable"}
    if host:
        previous = predecessors(opportunity.get("trace_artifact"), host.get("roi_window_ns", [None, None]),
                                host["apis"][0]["name"])
        if previous:
            result["host_predecessors"] = {"basis": "previous traced launch, copy or same API on host thread",
                                           "rows": previous}
        launches = [re.escape(row["operation"]) for row in previous if row["kind"] == "kernel-launch"]
        if launches:
            pattern = re.compile(r"\b(?:" + "|".join(launches) + r")\s*<<<")
            files = matching_backend(list(source_files([workspace])), {"tool": {"name": "nsight-systems"}})
            found = next(_match(files, pattern), None)
            if found:
                result["host_source"] = _window(*found, "preceding-kernel-launch-site")
        if kind == "kernel" or (kind == "repeated-operations" and result.get("host_source")):
            return result
    if kind == "application-phase":
        label = re.escape(opportunity["phase"])
        pattern = re.compile(r'"' + label + r'(?::)?(?:\\n)?"')
        relation = "application-timer-label"
        paths = source_files([workspace])
    else:
        selected = set(opportunity.get("targets") or [])
        index = index_path(run, cid)
        rows = [t for t in load(index, {}).get("targets", []) if t["id"] in selected and t.get("symbol")]
        symbols = [re.escape(t["symbol"]) for t in rows]
        if not symbols:
            return result | {"source_status": "launch-symbol-unresolved"}
        preferred = [REPO / t["source"]["path"] for t in rows if t.get("source", {}).get("path")]
        preferred = [p for p in preferred if p.is_file() and p.resolve().is_relative_to(workspace)]
        if kind == "kernel-family":
            for row in rows:
                found = locate_call_site(row["symbol"], preferred, context=0)
                if found:
                    return result | {"source": _window(found["path"], found["lines"], found["match"],
                                                        "family-call-site")}
            return result | {"source_status": "family-call-site-unresolved"}
        pattern = re.compile(r"\b(?:" + "|".join(symbols) + r")\s*<<<")
        paths = preferred or source_files([workspace])
        relation = "lexical-launch-site"
    found = next(_match(paths, pattern), None)
    if not found and kind == "repeated-operations" and preferred:
        found = next(_match(source_files([workspace]), pattern), None)
    return result | ({"source": _window(*found, relation)} if found else {"source_status": "marker-unresolved"})
