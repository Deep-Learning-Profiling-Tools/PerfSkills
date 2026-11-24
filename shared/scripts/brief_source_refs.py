"""Source references for compact optimization briefs."""


def source_references(baseline_targets, variant_targets):
    source_refs = []
    for target in baseline_targets:
        src = target.get("source") or {}
        slc = target.get("slice") or {}
        source_refs.append({"id": target.get("id"), "path": src.get("path"),
                            "lines": [src.get("start_line"), src.get("end_line")],
                            "slice": slc.get("path"),
                            "relation": (target.get("dependency") or {}).get("relation", "hotspot"),
                            "parent": (target.get("dependency") or {}).get("parent"), "scope": (target.get("resolution") or {}).get("scope"),
                            "confidence": (target.get("resolution") or {}).get("confidence")})
    variant_refs = []
    for target in variant_targets:
        src = target.get("source") or {}
        variant_refs.append({"id": target.get("id"), "symbol": target.get("symbol"),
                             "path": src.get("path"),
                             "lines": [src.get("start_line"), src.get("end_line")],
                             "slice": (target.get("slice") or {}).get("path"),
                             "relation": "existing-variant",
                             "parent": (target.get("dependency") or {}).get("parent"), "scope": (target.get("resolution") or {}).get("scope")})
    return source_refs, variant_refs
