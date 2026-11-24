"""Project literal normalized observations into bounded, provider-neutral drilldowns."""

from kernel_evidence import compact_ncu


def groups(profile, counters=None):
    provider = profile.get("provider_evidence") or {}
    result = {group: provider[key] for group, key in (
        ("timing", "operation_totals"), ("locations", "locations"), ("contexts", "contexts"),
        ("stalls", "stalls"), ("findings", "findings")) if provider.get(key)}
    if counters is None:
        counters = compact_ncu(profile) or {}
    if counters.get("stalls"):
        result.setdefault("stalls", counters["stalls"])
    facts = []
    for section, keys, unit in (
        ("throughput_pct", ("dram", "sm", "compute_memory", "l1", "l2"), "%"),
        ("memory", ("l2_hit_pct", "l1_hit_pct"), "%"),
        ("occupancy", ("achieved_pct",), "%"),
        ("scheduler", ("eligible_warps",), "warps"),
        ("scheduler", ("issue_active_pct",), "%"),
    ):
        values = counters.get(section) or {}
        facts.extend({"kernel": counters.get("kernel"), "metric": key, "value": values[key], "unit": unit}
                     for key in keys if values.get(key) is not None)
    if facts:
        result.setdefault("findings", facts)
    if counters.get("duration_ns") is not None:
        result.setdefault("timing", [{"kernel": counters.get("kernel"), "metric": "single-replay",
                                      "value": counters["duration_ns"], "unit": "ns"}])
    elif "timing" not in result:
        timed = [dict(kernel=h["name"], seconds=h["self_time_s"], calls=h.get("calls"))
                 for h in profile.get("hotspots", []) if h.get("self_time_s") is not None]
        if timed:
            result["timing"] = timed
    return result
