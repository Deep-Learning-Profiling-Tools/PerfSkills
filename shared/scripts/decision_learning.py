"""Learn the cost of delivering evidence and its association with subsequent edits.

Progress is measured against the best correct incumbent. Share credit among recent
views on the chosen branch. Neither a successful capture nor this association is
a causal estimate of the profiler's effect.
"""

import fcntl
import math
from contextlib import contextmanager

from experiment_support import load, write
from skill_binding import binding, digest, store_path


def active(plan):
    settings = (plan.get("profiling") or {}).get("skill_runtime") or {}
    return settings.get("enabled") and settings.get("decision_policy") in {"marginal-value-v1", "budgeted-lookahead-v2", "budgeted-lookahead-v3"}


def objective(plan):
    settings = plan["profiling"]["skill_runtime"]
    metric = plan.get("metric") or {}
    scope = metric.get("scope") or ("process" if metric.get("source") == "wall" else
            "declared-roi" if (plan.get("profiling") or {}).get("roi_name") else metric)
    from pathlib import Path
    selection = load(Path(plan["run_dir"]) / "experiment-state.json", {}).get("selection_objective")
    return digest([scope, metric.get("direction", "lower"), metric.get("unit"), settings.get("optimizer_id"),
                   "packet-v1"] + ([selection] if selection else []))


@contextmanager
def update(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        memory = load(path, {})
        yield memory
        write(path, memory)


def memory_path(plan, key):
    return store_path(plan, {"key": key}).with_suffix(".decisions.json")


def exposed(packet):
    obs = packet.get("observations") or {}
    counters = obs.get("counters") or {}
    caps = set()
    if (obs.get("timeline") or {}).get("sync_calls") is not None:
        caps.add("sync")
    if (obs.get("timeline") or {}).get("leading_launch_api"):
        caps.add("launch-overhead")
    for key, capability in (("occ_pct", "gpu-occupancy"), ("regs", "register-pressure"),
                            ("stall", "instruction-stalls"), ("dram_read_GB", "memory-bandwidth")):
        if counters.get(key) is not None:
            caps.add(capability)
    if (counters.get("throughput_pct") or {}).get("dram") is not None:
        caps.add("memory-bandwidth")
    if counters.get("cache_hit_pct"):
        caps.add("cache")
    for key, capability in (("kernel_time_s", "gpu-hotspot"), ("kernel_sum_ms", "gpu-hotspot"),
                            ("contexts", "call-context"), ("stalls", "instruction-stalls"),
                            ("copy_calls", "transfer")):
        if obs.get(key) is not None:
            caps.add(capability)
    locations = list(obs.get("locations") or [])
    stalls = [str(s.get("metric", "")).lower() for s in obs.get("stalls") or []]
    for finding in obs.get("findings") or []:
        locations.extend(finding.get("locations") or [])
        if finding.get("capability") == "value-redundancy" and finding.get("value") is not None:
            caps.add("value-redundancy")
        token = finding.get("stall_token")
        if token:
            caps.add("instruction-stalls")
            stalls.append(str(token).lower())
            if any(loc.get("file") and loc.get("line") for loc in finding.get("locations") or []):
                caps.add("causal-stall-attribution")
    if any(loc.get("pc_offset") is not None for loc in locations):
        caps.add("gpu-pc")
    if any(loc.get("file") and loc.get("line") for loc in locations):
        caps.add("gpu-source-line")
    if any(label in stall for stall in stalls for label in ("lat_gmem", "lat_tmem", "long_scoreboard")):
        caps.add("memory-latency")
    for name in obs.get("operations") or {}:
        if name.startswith("gsync"):
            caps.add("sync")
        if name.startswith(("gxcopy", "gicopy")):
            caps.add("transfer")
    from evidence_scope import roi_scope
    if roi_scope(obs.get("scope")):
        caps.add("roi-attribution")
    return caps


def delivery(plan, run, state, capsule, packet, tokens):
    if not active(plan) or capsule.get("status") not in {"complete", "partial"} or capsule.get("cache") == "hit":
        return
    runtime = state.get("skill_runtime") or {}
    recipe = runtime.get("last_recipe")
    cid = capsule.get("id")
    if (not recipe or runtime.get("last_capture") != cid or
            (capsule.get("ncu") or {}).get("disposition") in {"reused", "cache-hit"}):
        return
    acquisition = capsule.get("acquisition") or {}
    needs = set(acquisition.get("needs") or [])
    if not needs or not acquisition.get("provider"):
        return
    from skill_experience import observed
    fraction = len(observed(capsule) & exposed(packet) & needs) / len(needs)
    key = runtime["binding"]
    event = digest([str(run), cid, capsule.get("profile_artifact"), capsule.get("spent_s"), recipe])
    record_key = digest([recipe, objective(plan)])
    with update(memory_path(plan, key)) as memory:
        record = memory.setdefault(record_key, {})
        recent = record.setdefault("deliveries", [])
        if not any(sample["event"] == event for sample in recent):
            recent.append({"event": event, "tokens": tokens, "visible": fraction})
            del recent[:-32]
    if fraction:
        write(run / "candidates" / cid / "deliveries" / (event + ".json"),
              {"binding": key, "record": record_key, "objective": objective(plan),
               "batch": next((b["id"] for b in reversed(state.get("batches", []))
                              if cid in b.get("candidates", [])), None)})


def forwarded(plan, run, state, references, packet):
    reference = (packet.get("observations") or {}).get("reference_evidence") or {}
    if reference.get("observations"):
        import json
        from brief_facts import token_counter
        for capsule in references:
            if capsule["id"] == reference.get("id"):
                tokens = token_counter()[0](json.dumps(reference, separators=(",", ":")))
                delivery(plan, run, state, capsule, reference, tokens)


def incumbent_gain(row):
    if (not (row.get("correctness") or {}).get("passed") or
            ((row.get("ranking") or {}).get("status") in {"unstable", "invalid", "resolution-limited"} or
             (row.get("ranking") or {}).get("diagnostic_disagreement"))):
        return None
    point = (row.get("improvement") or {}).get("point")
    before = (row.get("ranking") or {}).get("incumbent_at_screen")
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (point, before)):
        return None
    return max(0., math.log(point / max(1., before)))


def feedback(plan, run, state, row):
    from rewrite_evidence import enabled, learn
    if enabled(plan):
        learn(plan, run, state, row)
    gain = incumbent_gain(row)
    if gain is None:
        return
    bound = binding(plan)
    cid = row["id"]
    parent = (state["candidates"].get(cid) or {}).get("parent")
    weights = {}
    for distance in (1, 2):
        if not parent:
            break
        batch = next((b["id"] for b in reversed(state.get("batches", []))
                      if parent in b.get("candidates", [])), None)
        for path in (run / "candidates" / parent / "deliveries").glob("*.json"):
            view = load(path, {})
            if view.get("binding") == bound["key"] and view.get("objective") == objective(plan) and view.get("batch") == batch:
                weights.setdefault(view["record"], 1 / distance)
        parent = (state["candidates"].get(parent) or {}).get("parent")
    if weights:
        event = digest([str(run), cid, row.get("metric"), row.get("ranking")])
        with update(memory_path(plan, bound["key"])) as memory:
            for record_key, weight in weights.items():
                samples = memory.setdefault(record_key, {}).setdefault("outcomes", {})
                samples[event] = [gain, weight / sum(weights.values())]
                while len(samples) > 64:
                    del samples[next(iter(samples))]
