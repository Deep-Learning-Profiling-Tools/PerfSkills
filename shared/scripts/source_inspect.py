"""Return a compact frontier or one bounded, already-emitted source slice."""

from pathlib import Path

import hashlib
import json
from brief_facts import token_counter
from experiment_support import REPO, load


def index_path(run, candidate):
    if candidate is None:
        return run / "source-targets.json"
    directory = run / "final-profile" if candidate == "final" else run / "candidates" / candidate / "profile"
    path = directory / "source-targets.json"
    return run / "source-targets.json" if candidate == "baseline" and not path.is_file() else path


def inspect(run, candidate=None, target=None, offset=0, limit=6, known=None):
    path = index_path(run, candidate)
    doc = load(path, {})
    rows = doc.get("targets", [])
    if target:
        row = next((x for x in rows if x["id"] == target), None)
        if not row:
            raise ValueError("unknown emitted source target: " + target +
                             "; use inspect --section source to find a target ID, then --target target-NN")
        source = Path(row["slice"]["path"])
        source = source if source.is_absolute() else REPO / source
        if not source.resolve().is_relative_to(run.resolve()):
            raise ValueError("source slice must belong to the requested run")
        lines = source.read_text().splitlines()
        end = min(len(lines), offset + 80)
        body_hash = hashlib.sha256("\\n".join(lines).encode()).hexdigest()[:16]
        if known == body_hash:
            return {"target": target, "snapshot": candidate or "baseline", "content_id": body_hash, "unchanged": True}
        count, _ = token_counter()
        while end > offset+1 and count(json.dumps(lines[offset:end])) > 1000:
            end -= 1
        return {"content_id": body_hash,"target": target, "snapshot": candidate or "baseline", "slice": str(source),
                "text": "\n".join(lines[offset:end]), "next_offset": end if end < len(lines) else None}
    chosen = rows[offset:offset + min(limit, 6)]
    return {"snapshot": candidate or "baseline", "total": len(rows), "artifact": str(path),
            "targets": [{"id": x["id"], "symbol": x.get("symbol"),
                         "relation": x.get("dependency", {}).get("relation", "hotspot"),
                         "parent": x.get("dependency", {}).get("parent"),
                         "same_source_as": next((y["id"] for y in rows[:rows.index(x)] if y.get("source") == x.get("source")), None),
                         "file": Path(x.get("source", {}).get("path", "")).name,
                         "lines": [x.get("source", {}).get(k) for k in ("start_line", "end_line")]}
                        for x in chosen],
            "next_offset": offset + len(chosen) if offset + len(chosen) < len(rows) else None}
