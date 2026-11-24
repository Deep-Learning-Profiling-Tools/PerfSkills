"""Save complete parent-relative edits once, before the agent needs a final ledger."""

from pathlib import Path
from experiment_support import load, sha256, write
from inspection_summary import patch_regions


def functions(path, positions):
    """Reuse lexical source location. Unresolved file-scope edits have no function."""
    from source_scan import locate_symbols
    from source_variants import _declared_names
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    names = _declared_names(lines, "")
    found = locate_symbols(names, [path], len(lines), 0)
    return sorted(name for name, row in found.items() if row and row["method"].startswith("function")
                  and any(row["start"] < line <= row["end"] for line in positions))


def build(plan, run, state, cid):
    meta = state["candidates"][cid]
    root = run / "candidates" / cid
    patch = root / "change.patch"
    output = root / "source-changes.json"
    fingerprint = meta.get("measurement_fingerprint")
    if not patch.is_file():
        return {"status": "unavailable", "candidate": cid, "reason": "no-saved-parent-patch"}
    signature = [sha256(patch), fingerprint, meta["parent"]]
    old = load(output, {})
    if old.get("signature") == signature:
        return old
    parent = Path(plan["source"]["baseline_dir"]) if meta["parent"] == "baseline" else Path(
        state["candidates"][meta["parent"]]["workspace"])
    workspace = Path(meta["workspace"])
    edits = patch_regions(run, cid, None, literal=True)
    files = []
    for name in dict.fromkeys(e["file"] for e in edits):
        ranges = [e for e in edits if e["file"] == name]
        before = [n for e in ranges for n in range(e["parent"][0], sum(e["parent"]))]
        after = [n for e in ranges for n in range(e["candidate"][0], sum(e["candidate"]))]
        functions_before = functions(parent / name, before)
        functions_after = functions(workspace / name, after)
        files.append({"file": name, "functions_parent": functions_before, "functions_candidate": functions_after,
                      "function_resolution": "lexical-source-location", "edits": ranges})
    result = {"status": "complete", "candidate": cid, "parent": meta["parent"], "signature": signature,
              "measurement_fingerprint": fingerprint, "patch": str(patch), "files": files}
    write(output, result)
    return result
