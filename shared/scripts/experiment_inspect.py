"""Bounded, named experiment evidence exposed through the single public CLI."""

from __future__ import annotations

from pathlib import Path

from agent_view_build import drilldown
from experiment_support import load
from kernel_runner import locations
from candidate_evidence import profile_for, compact_hotspots

GROUPS = {"launch", "occupancy", "scheduler", "stalls", "instructions", "control_flow",
          "memory", "throughput", "roofline", "rules", "source"}


def inspect(run: Path, section: str, limit: int, candidate: str | None = None,
            group: str | None = None, offset: int = 0) -> dict:
    limit = min(50, max(1, limit))
    if section == "costs":
        from inspect_cost import summarize
        return summarize(run)
    if section == "evidence":
        if not candidate:
            raise ValueError("evidence inspection requires --candidate")
        return evidence(run, candidate, group, limit)
    if section == "harness" and candidate in {None, "baseline"}:
        plan = load(run / "experiment-plan.json", {})
        if (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3":
            execution = plan.get("execution") or {}
            from selection_objective import summary
            return {"snapshot": "baseline", "selection_objective": summary(load(run / "experiment-state.json", {})),
                    "command": execution.get("command"),
                    "metric": plan.get("metric"), "oracle": plan.get("oracle"),
                    "application_reported": load(run / "application-facts.json", {}),
                    "baseline_timing": load(run / "baseline-timing.json", {}),
                    "baseline_acquisition": load(run / "baseline-acquisition.json", {}),
                    "compiled_skill": load(run / "compiled-skill.json", {}),
                    "skill_runtime": load(run / "experiment-state.json", {}).get("skill_runtime")}
    if section == "benchmarks" and candidate is None:
        state = load(run / "experiment-state.json", {})
        measured = [{"id": cid, "result": meta["result"]}
                    for cid, meta in (state.get("candidates") or {}).items() if meta.get("result")]
        if measured:
            if group != "detail":
                from inspection_summary import benchmarks
                return benchmarks(run, state, limit, offset)
            return {"candidates": measured[-limit:]}
    if candidate and section in {"hotspots", "source", "warnings", "routing", "benchmarks", "artifacts", "gates", "harness"}:
        profile, path = profile_for(run, candidate)
        if section == "hotspots":
            baseline, _ = profile_for(run, "baseline")
            return {"candidate": candidate, "status": "available" if profile else "unavailable",
                    "baseline": compact_hotspots(baseline, limit), "variant": compact_hotspots(profile, limit),
                    "scope": profile.get("scope"), "artifact": str(path)}
        directory = (run if candidate == "baseline" else run / "final-profile" if candidate == "final" else
                     run / "candidates" / candidate / "profile")
        if section == "source":
            doc = load(directory / "source-targets.json", {})
            if not doc:
                return {"status": "unavailable", "candidate": candidate, "targets": []}
            if (profile.get("scope") or {}).get("timing_join") == "historical-attribution-corrected-on-read":
                names = {h["name"] for h in profile.get("hotspots", [])}
                matched = [t for t in doc.get("targets", []) if t.get("hotspot") in names]
                return {"candidate": candidate, "status": "available" if matched else "stale-source-attribution",
                        "captured_symbols": sorted(names), "targets": matched[:limit]}
            return doc | {"candidate": candidate, "targets": doc.get("targets", [])[:limit]}
        if section == "warnings":
            return {"candidate": candidate, "warnings": profile.get("warnings", [])[:limit], "scope": profile.get("scope")}
        if section == "artifacts":
            return {"candidate": candidate, "profile": str(path) if profile else None,
                    "files": [str(p) for p in sorted(directory.glob("*.json"))[:limit]]}
        state = load(run / "experiment-state.json", {})
        meta = (state.get("candidates") or {}).get(candidate, {})
        if section == "gates":
            result = meta.get("result") or {}
            confirmed = (state.get("confirmed") or {}).get("id") == candidate
            return {"candidate": candidate, "status": meta.get("status", "not-evaluated"),
                    "correctness": result.get("correctness"), "improvement": result.get("improvement"),
                    "verified": confirmed, "confidence": result.get("confidence", "screening-only")}
        if section == "harness":
            capsule = meta.get("profile") or {}
            return {"candidate": candidate, "status": meta.get("status"),
                    "profile_status": capsule.get("status"), "reason": capsule.get("reason"),
                    "artifact": capsule.get("artifact"), "acquisition": capsule.get("acquisition")}
        if section == "benchmarks" and group != "detail":
            from inspection_summary import benchmarks
            return benchmarks(run, {"candidates": {candidate: meta}}, 1)
        return {"candidate": candidate, section: (meta.get("profile", {}).get("acquisition") if section == "routing"
                                                  else meta.get("result"))}
    if section == "routing":
        return load(run / "profiler-routing.json", {"status": "not-recorded"})
    if section != "ncu":
        return drilldown(run, section, limit)
    if not candidate or not group:
        raise ValueError("NCU inspection requires --candidate and --group")
    if group not in GROUPS:
        raise ValueError(f"unknown NCU group {group}; choose from {sorted(GROUPS)}")
    drilldowns = locations(run, candidate)[3]
    value = load(drilldowns / f"{group}.json")
    if not value:
        return {"status": "unavailable", "candidate": candidate, "group": group}
    value["candidate"] = candidate
    return value


def evidence(run: Path, candidate: str, group: str | None, limit: int) -> dict:
    profile, path = profile_for(run, candidate)
    packet = load(run / "candidates" / candidate / "evidence.json", {})
    if not group:
        return packet.get("agent") or {"candidate": candidate, "status": "unavailable", "artifact": str(path)}
    if group == "resources":
        from compiler_evidence import for_target
        doc = load(run / "compiler" / (candidate + ".json"), {})
        top = (profile.get("hotspots") or [{}])[0].get("name")
        static = (packet.get("detail") or packet.get("agent") or {}).get("static") or {}
        match = for_target(doc, top or static.get("target"))
        return {"candidate": candidate, "scope": "static-binary-resources",
                "execution_identity": "profile-symbol" if top else "historical-name-only",
                "target": match.get("active_target"), "kernels": match["kernels"][:limit]}
    if group == "routing":
        state = load(run / "experiment-state.json", {})
        return {"candidate": candidate, "routing": ((state.get("candidates") or {}).get(candidate, {}).get("profile") or {}).get("acquisition")}
    from capture_history import captures
    from evidence_views import groups
    if group not in {"contexts", "stalls", "findings", "timing", "locations"}:
        raise ValueError("evidence group must be resources, routing, contexts, stalls, findings, locations, or timing")
    state = load(run / "experiment-state.json", {})
    meta = (state.get("candidates") or {}).get(candidate) or {}
    identity = state.get("contract_sha256") if candidate == "baseline" else meta.get("measurement_fingerprint")
    history = captures(run, candidate, identity) if identity else ()
    from itertools import chain
    for document, artifact in chain([(profile, path)], history):
        items = groups(document).get(group)
        if items:
            return {"candidate": candidate, "tool": document.get("tool"), "scope": document.get("scope"),
                    "status": "available", "items": items[:limit], "artifact": str(artifact)}
    return {"candidate": candidate, "group": group, "status": "unavailable", "items": []}
