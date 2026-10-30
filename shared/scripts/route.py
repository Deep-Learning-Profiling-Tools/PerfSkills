#!/usr/bin/env python3
"""Inspect ProfileWeaver's capability router; experiments call the same router internally."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from profiler_registry import load, select

REPO = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO / "shared/config/registry.json"


def split_values(values):
    return {item.strip() for value in values for item in value.split(",") if item.strip()}


def check_registry(registry):
    markdown = (REPO / ".claude/skills/profile-weaver/references/registry.md").read_text()
    installed = markdown.split("## Installed", 1)[1].split("## Candidate children", 1)[0]
    documented = set(re.findall(r"\| `((?:pw-)[^`]+)` \|", installed))
    configured = {item["skill"] for item in registry["skills"]}
    directories = {path.name for path in (REPO / ".claude/skills").glob("pw-*")
                   if (path / "SKILL.md").exists()}
    result = {"status": "ok" if documented == configured == directories else "mismatch",
              "documented_only": sorted(documented - configured),
              "configured_only": sorted(configured - documented),
              "directory_only": sorted(directories - configured),
              "missing_directory": sorted(configured - directories)}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--need", action="append", default=[])
    parser.add_argument("--available", action="append", default=[])
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--tool", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--policy", type=Path, default=REPO / "runs/_knowledge/policy.json")
    parser.add_argument("--allow-unavailable", action="store_true")
    parser.add_argument("--check-registry", action="store_true")
    args = parser.parse_args()
    registry = load(args.registry)
    if args.check_registry:
        return check_registry(registry)
    needs = split_values(args.need)
    if not needs:
        parser.error("at least one --need is required")
    try:
        tools = dict(value.split("=", 1) for value in args.tool)
    except ValueError:
        parser.error("each --tool must be NAME=PATH")
    result = select(registry, needs, split_values(args.available),
                    split_values(args.artifact), tools, args.policy, args.allow_unavailable)
    print(json.dumps(result, separators=(",", ":")))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
