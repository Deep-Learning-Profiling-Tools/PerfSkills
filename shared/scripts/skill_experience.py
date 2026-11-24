"""Record observed evidence yield and solidify validated profiler recipes."""

import fcntl
import json
import math
import sys
from experiment_support import REPO, load, write
from skill_binding import enabled, digest, binding, context, store_path, recipe_key


def observed(capsule):
    """Only credit evidence explicitly emitted by the selected adapter."""
    caps = set(capsule.get("observed_capabilities") or [])
    ncu = capsule.get("ncu") or {}
    if ncu.get("status") in {None, "complete"}:
        if (ncu.get("occupancy") or {}).get("achieved_pct") is not None:
            caps.add("gpu-occupancy")
        if (ncu.get("launch") or {}).get("registers_per_thread") is not None:
            caps.add("register-pressure")
        if ncu.get("stalls"):
            caps.add("instruction-stalls")
        if any((ncu.get("memory") or {}).get(k) is not None for k in ("l1_hit_pct", "l2_hit_pct")):
            caps.add("cache")
        # Throughput, memory latency and divergence require their own observations.
        if ((ncu.get("throughput_pct") or {}).get("dram") is not None or
                any((ncu.get("throughput") or {}).get(k) is not None for k in ("dram_pct", "memory_pct"))):
            caps.add("memory-bandwidth")
        if ((ncu.get("roofline") or {}).get("achieved_dram_gbs") is not None or
                (ncu.get("duration_ns", 0) and any((ncu.get("memory") or {}).get(k) is not None
                 for k in ("dram_read_bytes", "dram_write_bytes")))):
            caps.add("memory-bandwidth")
        if (ncu.get("roofline") or {}).get("status") == "complete":
            caps.add("gpu-roofline")
    return caps


def remember(plan, run, state, capsule):
    if (not enabled(plan) or capsule.get("status") not in {"complete", "partial", "blocked", "inconclusive", "failed"}
            or capsule.get("cache") == "hit" or (capsule.get("ncu") or {}).get("disposition") in {"reused", "cache-hit"}):
        return
    acquisition = capsule.get("acquisition") or {}
    needs, provider = acquisition.get("needs") or [], acquisition.get("provider")
    seconds = capsule.get("spent_s")
    if not provider or not needs or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
        return
    bound = binding(plan)
    if not bound["observed"]:
        return
    candidate = {"provider": provider, "tool": acquisition["tool"]}
    nature = context(plan, state)
    key = recipe_key(candidate, needs, nature)
    actual = observed(capsule) if capsule.get("status") in {"complete", "partial"} else set()
    coverage = len(set(needs) & actual) / len(set(needs))
    # Evidence kinds and literal diagnostic findings; no timestamps/paths/durations.
    evidence = {"capabilities": sorted(actual),
                "findings": (capsule.get("provider_evidence") or {}).get("findings"),
                "target": (capsule.get("target") or {}).get("name"),
                "stalls": (capsule.get("provider_evidence") or {}).get("stalls") or (capsule.get("ncu") or {}).get("stalls"),
                "diagnosis": ((capsule.get("ncu") or {}).get("advice") or {}).get("primary")}
    signature = digest(evidence)
    path = store_path(plan, bound); path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        memory = load(path, {}) or {"revision": 0, "recipes": {}}
        record = memory["recipes"].setdefault(key, {"provider": provider, "needs": needs, "nature": nature,
             "attempts": 0, "coverage_sum": 0, "novelty_sum": 0, "wall_sum": 0,
             "signatures": [], "consecutive_failures": 0, "status": "learning"})
        event = digest([str(run), capsule.get("id"), capsule.get("artifact"),
                        capsule.get("profile_artifact"), seconds])
        if event in record.get("capture_events", []):
            return
        record["capture_events"] = (record.get("capture_events", []) + [event])[-256:]
        novelty = float(signature not in record["signatures"] and coverage > 0)
        record["attempts"] += 1
        record["coverage_sum"] += coverage
        record["novelty_sum"] += novelty
        record["wall_sum"] += seconds
        if capsule.get("status") == "complete" and not capsule.get("fallback_attempts"):
            wall = (load(run / "baseline-timing.json", {}).get("sample") or {}).get("wall_s")
            elapsed = capsule.get("acquisition_wall_s", seconds)
            record["success_costs"] = (record.get("success_costs", []) + [[elapsed, wall]])[-16:]
        record["signatures"] = list(dict.fromkeys(record["signatures"] + [signature]))[-32:]
        record["consecutive_failures"] = 0 if coverage == 1 else record["consecutive_failures"] + 1
        record["consecutive_successes"] = record.get("consecutive_successes", 0) + 1 if coverage == 1 else 0
        record["status"] = ("rolled-back" if record["consecutive_failures"] >= 2 else
                            "validated" if record["consecutive_successes"] >= 2 else "learning")
        memory["revision"] += 1
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(memory, sort_keys=True) + "\n")
        temporary.replace(path)
    state["skill_runtime"] = {"binding": bound["key"], "revision": memory["revision"],
                             "last_recipe": key, "coverage": coverage, "novelty": novelty,
                             "last_capture": capsule.get("id", "baseline"),
                             "recipe_status": record["status"]}
    write(run / "environment-binding.json", bound)
    compile_recipe(plan, run, state, capsule, key)


def compile_recipe(plan, run, state, capsule, key):
    """Solidify a typed invocation and its dependency graph, not generated shell."""
    acquisition = capsule["acquisition"]
    cid = capsule.get("id", "baseline")
    argv = [sys.executable, str(REPO / "shared/scripts/experiment.py"), str(run / "experiment-plan.json"),
            "evidence", cid]
    for need in acquisition["needs"]:
        argv += ["--need", need]
    argv += ["--provider", acquisition["provider"]]
    write(run / "compiled-skill.json", {"protocol": "profileweaver.compiled-skill/v1",
          "binding": state["skill_runtime"]["binding"], "revision": state["skill_runtime"]["revision"],
          "recipe": key, "status": state["skill_runtime"]["recipe_status"],
          "parameters": {"needs": acquisition["needs"], "provider": acquisition["provider"]},
          "argv": argv, "parameter_slots": ["candidate", "needs", "provider"],
          "promotion_requires": "two actual capable acquisitions under the same binding",
          "graph": dependency_graph([
              {"id": "binding", "inputs": ["environment"], "outputs": ["bound-tools"], "resource": "cpu"},
              {"id": "snapshot-validation", "inputs": ["bound-tools", "snapshot"], "outputs": ["validated-snapshot"], "resource": "cpu"},
              {"id": "static-resources", "inputs": ["bound-tools", "snapshot"], "outputs": ["resources"], "resource": "cpu"},
              {"id": "capture", "inputs": ["validated-snapshot", "resources"], "outputs": ["raw-profile"], "resource": "gpu-exclusive"},
              {"id": "reduce", "inputs": ["raw-profile"], "outputs": ["observations"], "resource": "cpu"},
              {"id": "learn", "inputs": ["observations"], "outputs": ["experience"], "resource": "cpu"}]),
          "execution": "existing typed adapters; sequential GPU captures; no additional benchmark runs",
          "limitation": "Observed evidence yield is a scheduling heuristic, not proof a profiler caused a speedup."})


def dependency_graph(steps):
    producers = {output: step["id"] for step in steps for output in step["outputs"]}
    return [step | {"requires": sorted({producers[item] for item in step["inputs"] if item in producers})}
            for step in steps]


def feedback(plan, run, state, row):
    """Weak observational credit only for the immediate, stably measured child.

    A failed rewrite is not evidence that its profiler was wrong. No causal claim
    is made; correctness failures and noisy rankings never train progress credit.
    """
    if not enabled(plan) or not (row.get("correctness") or {}).get("passed") or (
            row.get("ranking") or {}).get("status") in {"unstable", "invalid", "resolution-limited"}:
        return
    runtime = state.get("skill_runtime") or {}
    parent = (state.get("candidates", {}).get(row.get("id")) or {}).get("parent")
    if not parent or parent != runtime.get("last_capture"):
        return
    prior = (((state.get("candidates", {}).get(parent) or {}).get("result") or {}).get("metric") or {}).get("median")
    if parent == "baseline":
        prior = (row.get("baseline_metric") or {}).get("median")
    current = (row.get("metric") or {}).get("median")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (prior, current)):
        return
    ratio = prior / current if plan["metric"].get("direction", "lower") == "lower" else current / prior
    bound = binding(plan)
    if bound["key"] != runtime.get("binding"):
        return
    path = store_path(plan, bound)
    if not path.is_file():
        return
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        memory = load(path, {})
        record = (memory.get("recipes") or {}).get(runtime.get("last_recipe"))
        event = digest([run.name, row.get("id"), current])
        if not record or event in record.get("feedback_events", []):
            return
        record["progress_count"] = record.get("progress_count", 0) + 1
        record["progress_sum"] = record.get("progress_sum", 0) + max(0, min(1, math.log2(ratio)))
        record["feedback_events"] = (record.get("feedback_events", []) + [event])[-256:]
        record["progress_role"] = "unpaired-observational-association-not-causal"
        memory["revision"] += 1
        state["skill_runtime"]["revision"] = memory["revision"]
        temporary = path.with_suffix(".tmp");temporary.write_text(json.dumps(memory, sort_keys=True) + "\n");temporary.replace(path)

