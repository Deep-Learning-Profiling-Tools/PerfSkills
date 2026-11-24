"""Keep routine inspections focused on source changes, errors and measured outcomes."""

import json
import re
from pathlib import Path
from brief_facts import token_counter
from experiment_support import load


def patch_regions(run, cid, limit=4, *, literal=False):
    path = run / "candidates" / cid / "change.patch"
    if not path.is_file():
        return []
    file, regions, group, old, new = None, [], None, 0, 0
    for line in path.read_text().splitlines():
        if line.startswith("+++ candidate/"):
            file = line.removeprefix("+++ candidate/")
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if match:
            a,_,c,_ = match.groups()
            old,new,group = int(a),int(c),None
        elif file and line[:1] in {"+", "-"} and not line.startswith(("+++", "---")):
            if group is None:
                group = {"file": file, "parent": [old,0], "candidate": [new,0]}
                if literal:
                    group.update(removed=[], added=[])
                regions.append(group)
            index = "candidate" if line.startswith("+") else "parent"
            group[index][1] += 1
            if literal:
                group["added" if index == "candidate" else "removed"].append(line[1:])
            new += line.startswith("+")
            old += line.startswith("-")
        elif line.startswith(" "):
            old,new,group = old+1,new+1,None
    return regions[:limit]


def benchmarks(run, state, limit, offset=0):
    rows = [(cid, m) for cid,m in state.get("candidates", {}).items() if m.get("result")]
    items = []
    for cid,m in rows[offset:offset+min(limit,10)]:
        result = m["result"]
        record = {"id": cid, "parent": m["parent"], "mechanism": m.get("mechanism"),
                  "correct": (result.get("correctness") or {}).get("passed"), "runs": result.get("runs"),
                  "metric": (result.get("metric") or {}).get("median"),
                  "speedup": (result.get("improvement") or {}).get("point"),
                  "quality": (result.get("ranking") or {}).get("status")}
        if m.get("objective_result"):
            record["selection"] = {k:m["objective_result"].get(k) for k in ("seconds","speedup","status")}
        items.append(record)
    counter,_ = token_counter()
    while len(items)>1 and counter(json.dumps(items,separators=(",",":"))) > 700:
        items.pop()
    end = offset+len(items)
    return {"candidates": items, "total": len(rows), "next_offset": end if end<len(rows) else None,
            "detail": "inspect --section benchmarks --candidate ID --group detail"}


def failure(path, limit=3):
    """Read bounded diagnostics from both build streams (Ninja links on stdout)."""
    path = Path(path)
    error = re.compile(r"fatal error|error:|undefined reference|not declared|No such file|not found", re.I)
    selected = []
    for log in (path, path.with_name(path.name.replace(".stderr.txt", ".stdout.txt"))):
        if not log.is_file():
            continue
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 16384))
            for line in stream.read().decode(errors="replace").splitlines():
                if error.search(line) and line.strip() not in selected:
                    selected.append(line.strip())
    selected.sort(key=lambda line: "undefined reference" not in line)
    selected = selected[:limit]
    return " | ".join(line[:350] for line in selected)
