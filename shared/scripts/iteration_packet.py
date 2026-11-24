"""Apply the agent token budget without losing named underlying evidence."""

from copy import deepcopy
import json
from brief_facts import token_counter
from experiment_support import write


def _compact(value):
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v is not None and v != [] and v != {}}
    return value


def reference_observations(capsule):
    """Forward the measured incumbent's evidence when the last edit was a regression."""
    observations = (capsule.get("agent_packet") or {}).get("observations") or {}
    keep = ("target", "scope", "kernel_sum_ms", "kernel_time_s", "calls", "findings",
            "stalls", "locations", "contexts", "operations", "timeline")
    result = {k: observations[k] for k in keep if observations.get(k) is not None}
    counters = observations.get("counters") or {}
    if counters:
        result["counters"] = {k: counters[k] for k in ("throughput_pct", "cache_hit_pct", "stall",
                              "eligible", "issue_pct", "occ_pct", "regs") if k in counters}
    return result


def emit_packet(run, candidate_id, view, profile, path, max_tokens, *, compact=False, presentation=None):
    count, method = token_counter()
    agent = deepcopy(presentation if presentation is not None else view)
    if compact:
        from packet_layout import prepare
        agent = _compact(prepare(agent, candidate_id))
    omissions = []
    # Drop low-priority duplication, then optional counter fields. Preserve actual
    # findings/source, measurement status, provider identity, scope, and unknowns.
    removals = [("observations","counters",key) for key in
                ("spent_s","evidence_s","metrics","reason","profile","classification_source")]
    removals += [("acquisition", "opportunity"), ("acquisition", "cost_basis"), ("observations", "operation_totals"),
                ("observations", "operations"),
                ("observations", "counters", "gpa_alternatives"),
                ("observations", "counters", "roofline"), ("observations", "counters", "delta_base_pct"),
                ("observations", "counters", "delta_parent_pct"), ("drilldown",),
                ("observations", "counters", "comparison_scope"), ("observations", "counters", "reason")]
    def encoded():
        return json.dumps(agent, separators=(",", ":"), ensure_ascii=False)
    for keys in removals:
        if count(encoded()) <= max_tokens:
            break
        parent = agent
        for key in keys[:-1]:
            parent = parent.get(key, {})
        if keys[-1] in parent:
            del parent[keys[-1]]
            omissions.append(".".join(keys))
    if compact and count(encoded()) > max_tokens:
        # The lookup handle and first changed region remain available. Mechanism
        # evidence and producer/consumer context compete with duplicate metadata.
        for key in ("unknowns", "static", "drilldown"):
            if key in agent:
                del agent[key]
                omissions.append(key)
            if count(encoded()) <= max_tokens:
                break
        for key in ("changes", "frontier", "controls"):
            values = agent.get("source", {}).get(key) or []
            while len(values) > 1 and count(encoded()) > max_tokens:
                values.pop()
                omissions.append("source." + key + "[-1]")
    if count(encoded()) > max_tokens:
        for key in ("findings", "stalls", "counters"):
            records = agent["observations"].pop(key, None)
            if records is not None:
                omissions.append("observations." + key)
            if count(encoded()) <= max_tokens:
                break
    from evidence_presentation import visible
    visible(agent)
    tokens = count(encoded())
    if tokens > max_tokens:
        from packet_layout import minimum
        agent = minimum(agent)
        omissions.append("remaining-observations")
        tokens = count(json.dumps(agent, separators=(",", ":"), ensure_ascii=False))
    if tokens > max_tokens:
        agent.get("source", {}).pop("changes", None)
        agent["roi"] = {k: agent.get("roi", {}).get(k) for k in (
            "correct", "role", "measurement_role", "timing_disagreement") if k in agent.get("roi", {})}
        tokens = count(json.dumps(agent, separators=(",", ":"), ensure_ascii=False))
    write(run / "candidates" / candidate_id / "evidence.json", {"agent": agent, "detail": view,
          "context": {"tokens": tokens, "max_tokens": max_tokens, "method": method, "omitted": omissions},
          "profile_artifact": str(path) if profile else None})
    return agent, tokens
