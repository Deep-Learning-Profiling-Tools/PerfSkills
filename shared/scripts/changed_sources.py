"""Emit bounded current-source slices even when a candidate needs no profiling capture."""

import hashlib
from itertools import islice
from pathlib import Path
from experiment_support import load, write


def ensure(run, state, cid, edits):
    directory = run / "candidates" / cid / "profile"
    output = directory / "source-targets.json"
    saved = load(output, {})
    if saved.get("targets"):
        return saved["targets"]
    meta = state.get("candidates", {}).get(cid) or {}
    if not meta.get("workspace"):
        return []
    workspace = Path(meta["workspace"]).resolve()
    targets = []
    for edit in edits[:4]:
        path = (workspace / edit["file"]).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            continue
        line, count = edit["candidate"]
        start, end = max(0, line - 13), line + min(count + 12, 67)
        try:
            with path.open(errors="replace") as stream:
                lines = list(islice(stream, start, end))
        except OSError:
            continue
        if not lines:
            continue
        tid = f"change-{len(targets) + 1:02d}"
        dest = directory / "source-slices" / (tid + ".txt")
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = f"# Changed source; static evidence only\n# source: {path}:{start+1}-{start+len(lines)}\n"
        body += "".join(f"{index:6d} | {text.rstrip()}\n" for index, text in enumerate(lines, start + 1))
        dest.write_text(body)
        targets.append({"id": tid, "symbol": None, "hotspot": None,
                        "source": {"path": str(path), "start_line": start + 1, "end_line": start + len(lines)},
                        "dependency": {"relation": "changed-region", "parent": None},
                        "slice": {"path": str(dest), "bytes": len(body.encode()),
                                  "sha256_16": hashlib.sha256(body.encode()).hexdigest()[:16]}})
    write(output, {"protocol": "profileweaver.source-targets/v1", "snapshot": cid,
                   "basis": "changed-source-only-no-execution-evidence",
                   "measurement_fingerprint": meta.get("measurement_fingerprint"), "targets": targets})
    return targets
