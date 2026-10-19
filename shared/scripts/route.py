#!/usr/bin/env python3
"""Select the cheapest available ProfileWeaver child from a machine-readable registry."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO / "shared/config/registry.json"


def load(path):
    return json.loads(path.read_text())


def split_values(values):
    return {item.strip() for value in values for item in value.split(",") if item.strip()}


def available(skill, forced, artifacts, tool_paths):
    if forced:
        return skill["skill"] in forced, ["explicit --available set"]
    checks, ok = [], True
    spec = skill.get("availability", {})
    for command in spec.get("commands_all", []):
        candidate = tool_paths.get(command)
        found = (str(Path(candidate).resolve()) if candidate and Path(candidate).is_file()
                 and os.access(candidate, os.X_OK) else shutil.which(command))
        checks.append(f"{command}={found if found else 'missing'}")
        ok &= found is not None
    for name in spec.get("env_all", []):
        present = bool(os.environ.get(name))
        checks.append(f"{name}={'set' if present else 'unset'}")
        ok &= present
    artifact = spec.get("artifact")
    if artifact:
        present = artifact in artifacts
        checks.append(f"artifact:{artifact}={'supplied' if present else 'missing'}")
        ok &= present
    return bool(ok), checks


def learned_adjustment(skill_name, policy_path):
    if not policy_path or not policy_path.exists():
        return 0, None, None
    policy = load(policy_path)
    entry = (policy.get("skills") or {}).get(skill_name)
    if not entry or not entry.get("eligible"):
        return 0, "learning prior unavailable (<3 recorded runs)", None
    # Learning may break a cost tie; it never overrides capability or availability.
    median_overhead = entry.get("median_overhead_factor")
    budget = {"profiled_runs": entry.get("median_profiled_runs"),
              "benchmark_reps": entry.get("median_benchmark_reps"),
              "watch_gates": entry.get("common_failed_gates") or []}
    return ((float(median_overhead or 1) - 1) / 100,
            f"measured cost prior from {entry['runs']} runs", budget)


def check_registry(registry):
    markdown = (REPO / ".claude/skills/profile-weaver/references/registry.md").read_text()
    installed = markdown.split("## Installed", 1)[1].split("## Candidate children", 1)[0]
    documented = set(re.findall(r"\| `((?:pw-)[^`]+)` \|", installed))
    configured = {x["skill"] for x in registry["skills"]}
    directories = {x.name for x in (REPO / ".claude/skills").glob("pw-*") if (x / "SKILL.md").exists()}
    result = {
        "status": "ok" if documented == configured == directories else "mismatch",
        "documented_only": sorted(documented - configured),
        "configured_only": sorted(configured - documented),
        "directory_only": sorted(directories - configured),
        "missing_directory": sorted(configured - directories),
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "ok" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--need", action="append", default=[], help="repeat or comma-separate capability tags")
    parser.add_argument("--available", action="append", default=[], help="explicit available skills; bypasses probes")
    parser.add_argument("--artifact", action="append", default=[], help="available pre-captured artifact kind")
    parser.add_argument("--tool", action="append", default=[], metavar="NAME=PATH",
                        help="verified command outside PATH, for example nsys=/opt/nvidia/nsys")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--policy", type=Path, default=REPO / "runs/_knowledge/policy.json")
    parser.add_argument("--allow-unavailable", action="store_true")
    parser.add_argument("--check-registry", action="store_true")
    args = parser.parse_args()
    registry = load(args.registry)
    if args.check_registry:
        return check_registry(registry)
    needs, forced, artifacts = split_values(args.need), split_values(args.available), split_values(args.artifact)
    try:
        tool_paths = dict(value.split("=", 1) for value in args.tool)
    except ValueError:
        parser.error("each --tool must be NAME=PATH")
    if not needs:
        parser.error("at least one --need is required")

    candidates = []
    for skill in registry["skills"]:
        capability = needs & set(skill["answers"])
        is_available, checks = available(skill, forced, artifacts, tool_paths)
        learned, prior, learned_budget = learned_adjustment(skill["skill"], args.policy)
        candidates.append({
            "skill": skill["skill"], "tool": skill["tool"], "covers": sorted(capability),
            "missing": sorted(needs - set(skill["answers"])), "available": is_available,
            "checks": checks, "cost_score": skill["cost_rank"] + learned, "prior": prior,
            "learned_budget": learned_budget, "full_loop": skill.get("full_loop", False),
        })
    pool = [x for x in candidates if x["covers"] and (x["available"] or args.allow_unavailable)]
    if not pool:
        result = {"status": "blocked", "needs": sorted(needs), "selected": None,
                  "reason": "no installed available child can observe a requested capability",
                  "probes": [{"skill": x["skill"], "checks": x["checks"]} for x in candidates if x["covers"]]}
        print(json.dumps(result, separators=(",", ":")))
        return 2
    pool.sort(key=lambda x: (-len(x["covers"]), len(x["missing"]), x["cost_score"], x["skill"]))
    winner = pool[0]
    status = "selected" if not winner["missing"] else "partial"
    result = {"status": status, "needs": sorted(needs), "selected": winner,
              "reason": "maximum requested coverage, then lowest measured/configured cost",
              "next": None if not winner["missing"] else {"unanswered": winner["missing"], "action": "profile, then reroute narrowed question"}}
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
