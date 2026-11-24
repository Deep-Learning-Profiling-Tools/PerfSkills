"""Environment-bound, versioned profiler recipes learned from actual acquisitions.


Only declarative recipe selection evolves. This module never generates Python,
changes application code, executes an LLM, or relaxes a measurement gate.
"""

from skill_experience import observed, remember, compile_recipe, dependency_graph, feedback
from skill_binding import enabled, digest, binding, context, store_path, recipe_key

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys

from experiment_support import REPO, load, write


def rank(plan, state, acquisition, needs, candidates):
    """Recompile a provider preference from coverage, novelty and measured cost.

    Cost priors are never represented as seconds. A learned preference requires
    two real, comparable attempts; an explicit provider always keeps authority.
    """
    if not enabled(plan) or acquisition.get("requested_provider"):
        return None
    bound = binding(plan)
    if not bound["observed"]:
        return None
    memory = load(store_path(plan, bound), {})
    nature = context(plan, state)
    scored = []
    for candidate in candidates:
        key = recipe_key(candidate, needs, nature)
        record = (memory.get("recipes") or {}).get(key) or {}
        attempts = record.get("attempts", 0)
        if attempts < 2 or record.get("status") not in {"validated", "rolled-back"}:
            continue
        coverage = record["coverage_sum"] / attempts
        novelty = record["novelty_sum"] / attempts
        seconds = record["wall_sum"] / attempts
        # A repeated confirming capture can still be useful; novelty is not truth.
        progress = record.get("progress_sum", 0) / max(1, record.get("progress_count", 0))
        utility = coverage * (.5 + .5 * novelty) * (1 + .1 * progress)
        scored.append({"candidate": candidate, "recipe": key, "attempts": attempts,
                       "coverage": coverage, "novelty": novelty, "wall_s": seconds,
                       "downstream_association": progress,
                       "utility_per_second": utility / seconds if seconds > 0 else 0,
                       "quarantined": record.get("consecutive_failures", 0) >= 2})
    # Compare validated alternatives without forcing calibration runs for every
    # installed tool. Unexplored providers stay available for distinctive needs.
    failed = {r["candidate"]["skill"] for r in scored if r["quarantined"]}
    eligible = [c for c in candidates if c["skill"] not in failed]
    learned = [r for r in scored if not r["quarantined"]]
    if failed and eligible:
        chosen = (max(learned, key=lambda r: r["utility_per_second"])["candidate"] if learned else
                  min(eligible, key=lambda c: (c.get("measured_s") is None,
                      c.get("measured_s") if c.get("measured_s") is not None else c["cost_rank"], c["skill"])))
        reason = "rollback-failed-recipe"
    elif len(learned) >= 2 or (len(learned) == len(candidates) and learned):
        chosen = max(learned, key=lambda r: r["utility_per_second"])["candidate"]
        reason = "learned-evidence-yield-per-second"
    else:
        return None
    return {"candidate": chosen, "reason": reason, "binding": bound["key"],
            "revision": memory.get("revision", 0),
            "unexplored": [c["skill"] for c in candidates if c["skill"] not in {r["candidate"]["skill"] for r in scored}],
            "scores": [{k: v for k, v in r.items() if k != "candidate"} | {"provider": r["candidate"]["skill"]}
                       for r in scored]}


def reuse_recipe(plan, run):
    """Reuse typed parameters only after validation; rebind and reroute on drift."""
    compiled = load(run / "compiled-skill.json", {})
    if not compiled.get("parameters"):
        raise ValueError("no compiled profiler recipe; request the missing capability once")
    parameters = compiled["parameters"]
    bound = binding(plan)
    memory = load(store_path(plan, bound), {})
    record = (memory.get("recipes") or {}).get(compiled.get("recipe")) or {}
    valid = compiled.get("binding") == bound["key"] and record.get("status") == "validated"
    return {"needs": parameters["needs"], "provider": parameters["provider"] if valid else None,
            "binding_status": "validated-reuse" if valid else "recompile-and-route"}


def questions(plan, state, seen):
    """Reorder unresolved views using environment-matched acquisition experience."""
    nature = context(plan, state)
    options = ([["sync", "launch-overhead"], ["transfer"], ["call-context"]]
               if "gpu-idle-visible" in nature else
               [["instruction-stalls"], ["memory-bandwidth", "gpu-occupancy"], ["causal-stall-attribution"]])
    if "gvprof" in ((plan.get("profiling") or {}).get("artifacts") or []):
        options.append(["value-redundancy"])
    bound = binding(plan)
    memory = load(store_path(plan, bound), {}) if bound["observed"] else {}
    remaining = []
    for index, option in enumerate(options):
        if set(option) <= seen:
            continue
        records = [r for r in memory.get("recipes", {}).values()
                   if set(r["needs"]) == set(option) and r["nature"] == nature
                   and r["status"] == "validated"]
        score = max((len(set(option) - seen) * r["coverage_sum"] / r["wall_sum"] *
                     (.5 + .5 * r["novelty_sum"] / r["attempts"])
                     for r in records if r["wall_sum"] > 0), default=None)
        remaining.append((option, score, index))
    # Learned seconds are compared only with seconds. Uncalibrated questions use
    # a disclosed prior; no predicted information gain is passed off as measured.
    remaining.sort(key=lambda r: (r[1] is None, -(r[1] or 0), r[2]))
    return [q for q, _, _ in remaining]


def replan(plan, state, row, acquisition):
    """Refine a justified question; missing counters alone never launch a profiler."""
    if not enabled(plan) or acquisition.get("tool") == "none" or acquisition.get("reason") != "plateau-needs-mechanism-evidence":
        return acquisition
    if not (row.get("correctness") or {}).get("passed"):
        return acquisition
    budget = (plan.get("budget") or {}).get("iterations")
    if budget and sum(bool(m.get("result")) for m in state.get("candidates", {}).values()) >= budget:
        return acquisition
    nature = context(plan, state)
    if "dominant-kernel" not in nature and "gpu-idle-visible" not in nature:
        return acquisition
    mechanism = ({"sync", "launch-overhead", "call-context"} if "gpu-idle-visible" in nature else
                 {"instruction-stalls", "memory-bandwidth", "memory-latency", "causal-stall-attribution", "value-redundancy"})
    cid = row.get("id"); parent = (state.get("candidates", {}).get(cid) or {}).get("parent", "baseline")
    # Preserve a recent, named ancestor diagnosis; do not import another branch's facts.
    seen = set()
    for _ in range(3):
        meta = state.get("candidates", {}).get(parent) or {}
        capsule = meta.get("profile") or (state.get("baseline_evidence") if parent == "baseline" else {}) or {}
        if capsule.get("status") == "complete":
            seen |= observed(capsule)
        if parent == "baseline":
            break
        parent = meta.get("parent", "baseline")
    if seen & mechanism:
        return {"tool": "none", "reason": "recent-ancestor-mechanism-evidence-still-valid",
                "screening_only": True}
    alternatives = questions(plan, state, seen)
    if not alternatives:
        return acquisition
    return {"tool": "kernel", "needs": alternatives[0], "question_alternatives": alternatives,
            "reason": acquisition["reason"], "question_origin": "policy",
            "screening_only": True, "recompiled_from": acquisition.get("reason")}
