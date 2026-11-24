"""Connect a literal delivered fact to an agent's hypothesis and its measured outcome."""

import re
from decision_learning import incumbent_gain, memory_path, objective, update
from experiment_support import load, write
from skill_binding import binding, context, digest

VERSION = "budgeted-lookahead-v2"


def enabled(plan):
    runtime = plan.get("profiling", {}).get("skill_runtime") or {}
    return runtime.get("enabled") and runtime.get("decision_policy") in {VERSION, "budgeted-lookahead-v3"}


def lineage(state, cid):
    seen = set()
    while cid and cid not in seen:
        seen.add(cid)
        yield cid
        cid = (state.get("candidates", {}).get(cid) or {}).get("parent")


def proposal(plan, run, state, parent, args):
    ids = sorted(set(getattr(args, "because", None) or []))
    choices = getattr(args, "consider", None) or []
    if not (ids or choices):
        return {}
    if not enabled(plan):
        raise ValueError("hypothesis links require a new lookahead plan")
    alternatives = {}
    for choice in choices:
        need, sep, edit = choice.partition("=")
        if not sep or not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", need) or not edit.strip():
            raise ValueError("--consider expects capability=rewrite hypothesis")
        alternatives[need] = edit.strip()
    for key in ids:
        if not re.fullmatch(r"[0-9a-f]{16}", key):
            raise ValueError("invalid evidence id")
        fact = load(run / "facts" / (key + ".json"), {})
        if fact.get("snapshot") not in lineage(state, parent):
            raise ValueError("cited evidence must have been delivered on the chosen branch")
        snapshot = fact["snapshot"]
        fingerprint = identity(state, snapshot)
        if not fingerprint or fingerprint != fact.get("fingerprint"):
            raise ValueError("cited source snapshot has been superseded")
    return {"hypothesis": {"because": ids, "alternatives": alternatives}}


def identity(state, cid):
    if cid == "baseline":
        return state.get("contract_sha256")
    return (state.get("candidates", {}).get(cid) or {}).get("measurement_fingerprint")


def inspect_decision(run, section, cid, target):
    if section == "fact":
        if not re.fullmatch(r"[0-9a-f]{16}", target or ""):
            raise ValueError("inspect --section fact requires --target FACT_ID")
        return load(run / "facts" / (target + ".json"), {"status": "unknown-fact"})
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", cid or ""):
        raise ValueError("inspect --section decision requires --candidate ID")
    root = run / "candidates" / cid
    decision = load(root / "acquisition-decision.json", {})
    modelled = [o for o in decision.get("options", []) if "net_value" in o]
    best = max(modelled, key=lambda o: o["net_value"], default=None)
    meta = load(run / "experiment-state.json", {}).get("candidates", {}).get(cid, {})
    actual = decision.get("selected") or (meta.get("profile") or {}).get("acquisition") or {}
    row = meta.get("result") or {}
    return {"snapshot": decision.get("snapshot", cid), "trigger": decision.get("trigger", actual.get("reason")),
            "action": actual, "highest_scored_capture": best,
            "hypothesis": load(root / "candidate.json", {}).get("hypothesis"),
            "outcome": load(root / "rewrite-outcome.json", {}) or ({"timing_quality": (row.get("ranking") or {}).get("status"),
                        "screen_speedup": (row.get("improvement") or {}).get("point"),
                        "incumbent_log_gain": None, "credit": "uncredited"} if row else None)}


def learn(plan, run, state, row):
    meta = state["candidates"].get(row["id"]) or {}
    hypothesis = meta.get("hypothesis") or {}
    gain = incumbent_gain(row)
    if gain is None or not hypothesis.get("because"):
        return
    bound = binding(plan)
    facts = [load(run / "facts" / (key + ".json"), {}) for key in hypothesis["because"]]
    facts = [f for f in facts if f.get("binding") == bound["key"] and
             f.get("fingerprint") == identity(state, f["snapshot"])]
    if not facts:
        return
    evidence = {"mechanism": meta["mechanism"], "because": hypothesis["because"],
                "fingerprint": identity(state, row["id"]), "incumbent_log_gain": gain}
    write(run / "candidates" / row["id"] / "rewrite-outcome.json", evidence)
    with update(memory_path(plan, bound["key"]).with_suffix(".rewrites.json")) as memory:
        events = {f["event"] for f in facts}
        for f in facts:
            record = memory.get(f["group"] + f["arm"], {}).get(f["event"])
            if record is not None:
                record["gain"] = max(record.get("gain", 0.), gain / len(events))
        for provider in {f["provider"] for f in facts}:
            key = digest([objective(plan), context(plan, state), provider, meta["mechanism"]])
            records = memory.setdefault("hypothesis-" + key, {})
            records[digest([str(run), row["id"]])] = gain / len(events)
            if len(records) > 64:
                del records[next(iter(records))]
