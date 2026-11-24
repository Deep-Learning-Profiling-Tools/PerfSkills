"""Fit measured facts to the optimizer's packet and record only the facts it receives."""

import math
from copy import deepcopy
from decision_learning import memory_path, objective, update
from experiment_support import load, write
from rewrite_evidence import enabled, identity
from skill_binding import binding, context, digest


def baseline_packet(run, state):
    plan = load(run / "experiment-plan.json", {})
    if not enabled(plan):
        return {}
    profile = load(run / "profile.baseline.json", {})
    top = (profile.get("hotspots") or [{}])[0]
    if top.get("self_time_s") is None:
        return {}
    provider = (profile.get("tool") or {}).get("name")
    view = {"observations": {"target": top.get("name"), "kernel_time_s": top["self_time_s"],
                            "scope": {k: v for k, v in (profile.get("scope") or {}).items()
                                      if k in {"policy", "name", "roi_name"}}}}
    provider = {"cupti-activity": "pw-cupti", "nsight-systems": "pw-nsight-systems"}.get(provider, provider)
    receipts = present(plan, run, state, "baseline", view, provider)
    if not receipts:
        return {}
    import json
    from brief_facts import token_counter
    delivered(plan, run, view, receipts, token_counter()[0](json.dumps(view, separators=(",", ":"))))
    return {"baseline_observation": view}


def representation(history, group):
    """Try both faithful displays, then prefer higher credited gain per packet token."""
    arms = ["focused", "context"]
    def utility(arm):
        samples = list(history.get(group + arm, {}).values())
        if not samples:
            return float("inf")
        gain = sum(s.get("gain", 0.) for s in samples)
        tokens = sum(s["tokens"] for s in samples)
        return (gain + .02 * math.sqrt(len(samples))) / max(1, tokens)
    return max(arms, key=utility)


def present(plan, run, state, cid, view, provider):
    obs = view["observations"]
    if not identity(state, cid) or not obs.get("target") or not provider:
        return {}
    bound = binding(plan)
    path = memory_path(plan, bound["key"]).with_suffix(".rewrites.json")
    panel = obs.get("counters") or {}
    fields = [("counters." + k, panel[k]) for k in
              ("stall", "throughput_pct", "regs", "occ_pct", "cache_hit_pct") if panel.get(k) is not None]
    fields += [(k, obs[k]) for k in ("findings", "stalls", "contexts", "operations", "kernel_time_s",
                                     "kernel_sum_ms", "copy_calls") if obs.get(k) is not None]
    if not fields:
        return {}
    group = digest([objective(plan), context(plan, state), provider, [k for k, _ in fields]])
    selection = run / "candidates" / cid / "presentations" / (digest([group, identity(state, cid), fields]) + ".json")
    arm = load(selection, {}).get("arm") or representation(load(path, {}) if bound["observed"] else {}, group)
    if not selection.exists():
        write(selection, {"arm": arm})
    if arm == "focused" and panel:
        # Focus on actionable readings while the context arm includes comparative
        # counters, neighboring source and collateral deltas. Both retain raw detail.
        keep = {"stall", "throughput_pct", "regs", "occ_pct", "cache_hit_pct", "eligible", "issue_pct",
                "roofline", "dram_read_GB", "dram_write_GB", "duration_ns", "kernel"}
        view["observations"]["counters"] = {k: v for k, v in panel.items() if k in keep}
        view.get("source", {}).pop("frontier", None)
    elif arm == "focused" and not ({"contexts", "locations"} & obs.keys()):
        for extra in ("calls", "launches"):
            view["observations"].pop(extra, None)
    receipts = {}
    for metric, value in fields[:2]:
        scope = obs.get("activity_totals_scope") if metric == "copy_calls" else obs.get("scope")
        fact = {"snapshot": cid, "fingerprint": identity(state, cid), "provider": provider,
                "target": obs["target"], "scope": scope, "metric": metric, "value": value,
                "arm": arm, "group": group, "binding": bound["key"]}
        receipts[digest(fact)[:16]] = fact
    view["cite"] = {f["metric"]: key for key, f in receipts.items()}
    return receipts


def visible(packet):
    """Discard handles whenever token truncation removed their literal value."""
    obs = packet["observations"]
    ref = obs.get("reference_evidence") or {}
    if ref.get("cite"):
        visible(ref)
    cite = packet.get("cite", {})
    for metric in list(cite):
        source = obs
        for part in metric.split("."):
            source = source.get(part) if isinstance(source, dict) else None
        if source is None:
            del cite[metric]
    if not cite:
        packet.pop("cite", None)


def delivered(plan, run, packet, receipts, tokens):
    keys = sorted((packet.get("cite") or {}).values())
    if not keys:
        return
    facts = [receipts[k] for k in keys]
    event = digest([str(run), keys])
    for key, fact in zip(keys, facts):
        write(run / "facts" / (key + ".json"), fact | {"event": event})
    fact = facts[0]
    with update(run / "candidates" / fact["snapshot"] / "issued-facts.json") as issued:
        issued.update(dict.fromkeys(keys, event))
    with update(memory_path(plan, fact["binding"]).with_suffix(".rewrites.json")) as memory:
        records = memory.setdefault(fact["group"] + fact["arm"], {})
        records.setdefault(event, {"tokens": tokens})
        if len(records) > 64:
            del records[next(iter(records))]



def prepare(plan, run, state, cid, view, provider, acquired):
    packet = deepcopy(view)
    ref = packet["observations"].get("reference_evidence") or {}
    if not acquired and ref.get("observations"):
        return packet, present(plan, run, state, ref["id"], ref, ref.get("provider"))
    return packet, present(plan, run, state, cid, packet, provider) if acquired else {}


def record(plan, run, packet, receipts, tokens):
    ref = packet.get("observations", {}).get("reference_evidence") or {}
    actual = ref if ref.get("cite") else packet
    if ref.get("cite"):
        import json
        from brief_facts import token_counter
        tokens = token_counter()[0](json.dumps(ref, separators=(",", ":")))
    delivered(plan, run, actual, receipts, tokens)
