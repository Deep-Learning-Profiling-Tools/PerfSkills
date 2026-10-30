#!/usr/bin/env python3
"""Resolve measured hotspots and their dependencies to small source ranges."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from source_dependencies import append_dependencies
from source_variants import append_variant, discover
from source_scan import (direct_location, load, locate_call_site, locate_symbol, repo_relative,
                         source_files, write_slice)
from source_symbols import symbol_candidates


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
    parser.add_argument("--dependency-depth", type=int, default=0,
                        help="bounded call/data dependency levels after the measured hotspot")
    parser.add_argument("--max-dependencies", type=int, default=0,
                        help="maximum extra callee/declaration/writer slices")
    parser.add_argument("--max-variants", type=int, default=0,
                        help="maximum existing sibling implementations to expose")
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
        requested = ([hotspot.get("name")] if explicit_mode else
                     symbol_candidates(hotspot.get("name", "")))
        symbol = requested[0] if requested else None
        found = None if explicit_mode else direct_location(
            hotspot, repo, search_roots, args.max_lines, args.context)
        if found is None:
            for candidate in requested:
                if explicit_mode == "call-site":
                    found = locate_call_site(candidate, files, args.context)
                else:
                    found = locate_symbol(candidate, files, args.max_lines, args.context)
                if found is not None:
                    symbol = candidate
                    break
        if found is None:
            unresolved.append({"hotspot": hotspot.get("name"), "symbols": requested})
            continue
        target_id = f"target-{len(targets) + 1:02d}"
        slice_path = slices_dir / f"{target_id}.txt"
        byte_count, digest = write_slice(slice_path, found["path"], found["lines"], found["start"], found["end"], hotspot.get("name"))
        targets.append({
            "id": target_id,
            "hotspot": hotspot.get("name"),
            "wall_pct": hotspot.get("pct_of_wall"),
            "symbol": symbol,
            "requested_symbols": requested,
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

    # Existing sibling implementations are a bounded search-space hint.  They are emitted
    # after measured hotspots and before callee/data dependencies so the agent can inspect a
    # known structural alternative without reading the whole source file.
    if args.max_variants > 0:
        variants = []
        for target in list(targets):
            if len(variants) >= args.max_variants:
                break
            if (target.get("dependency") or {}).get("relation") == "existing-variant":
                continue
            source = target.get("source") or {}
            path = Path(source.get("path", ""))
            if not path.is_absolute():
                path = repo / path
            if not path.is_file():
                continue
            for item in discover(target.get("symbol"), path, files, args.max_lines, args.context,
                                 args.max_variants - len(variants)):
                target_id = f"target-{len(targets) + 1:02d}"
                variant = append_variant(item, target, target_id, slices_dir, repo)
                targets.append(variant)
                variants.append(variant)
                if len(variants) >= args.max_variants:
                    break

    append_dependencies(targets, files, slices_dir, repo, args.dependency_depth,
                        args.max_dependencies, args.max_lines, args.context)

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
