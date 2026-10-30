"""Capability, availability, and cost-based profiler selection."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text())


def available(skill, forced=frozenset(), artifacts=frozenset(), tool_paths=None):
    tool_paths = tool_paths or {}
    if forced:
        return skill["skill"] in forced, ["explicit available set"]
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
    entry = (load(policy_path).get("skills") or {}).get(skill_name)
    if not entry or not entry.get("eligible"):
        return 0, "learning prior unavailable (<3 recorded runs)", None
    overhead = float(entry.get("median_overhead_factor") or 1)
    budget = {"profiled_runs": entry.get("median_profiled_runs"),
              "benchmark_reps": entry.get("median_benchmark_reps"),
              "watch_gates": entry.get("common_failed_gates") or []}
    return (overhead - 1) / 100, f"measured cost prior from {entry['runs']} runs", budget


def select(registry, needs, forced=frozenset(), artifacts=frozenset(), tool_paths=None,
           policy_path=None, allow_unavailable=False):
    needs = set(needs)
    candidates = []
    for skill in registry["skills"]:
        capability = needs & set(skill["answers"])
        present, checks = available(skill, forced, artifacts, tool_paths)
        learned, prior, budget = learned_adjustment(skill["skill"], policy_path)
        candidates.append({
            "skill": skill["skill"], "tool": skill["tool"], "covers": sorted(capability),
            "missing": sorted(needs - set(skill["answers"])), "available": present,
            "checks": checks, "cost_score": skill["cost_rank"] + learned,
            "prior": prior, "learned_budget": budget, "full_loop": skill.get("full_loop", False),
        })
    pool = [item for item in candidates
            if item["covers"] and (item["available"] or allow_unavailable)]
    if not pool:
        return {"status": "blocked", "needs": sorted(needs), "selected": None,
                "reason": "no installed available child can observe a requested capability",
                "probes": [{"skill": x["skill"], "checks": x["checks"]}
                           for x in candidates if x["covers"]]}
    pool.sort(key=lambda x: (-len(x["covers"]), len(x["missing"]), x["cost_score"], x["skill"]))
    winner = pool[0]
    return {"status": "selected" if not winner["missing"] else "partial",
            "needs": sorted(needs), "selected": winner,
            "reason": "maximum requested coverage, then lowest measured/configured cost",
            "next": (None if not winner["missing"] else
                     {"unanswered": winner["missing"],
                      "action": "profile, then reroute narrowed question"})}
