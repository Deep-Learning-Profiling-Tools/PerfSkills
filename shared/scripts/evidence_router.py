"""Question-first routing across installed, executable evidence adapters.

A configured cost is a prior, never a measured duration. An unavailable adapter
cannot become a successful profile or silently fall back to an incapable tool.
"""

from __future__ import annotations

import os
from pathlib import Path

from profiler_registry import available
from experiment_support import REPO, load
from native_profiler_probe import PROVIDERS, probe as native_probe
from evidence_opportunity import with_cost
from evidence_scope import requires_roi
from acquisition_limits import bounded, limited, remaining


def fresh_calibration(capsule):
    return (capsule.get("status") == "complete" and capsule.get("cache") != "hit"
            and not capsule.get("fallback_attempts")
            and (capsule.get("ncu") or {}).get("disposition") not in {"reused", "cache-hit"})


@bounded
def route(plan: dict, state: dict, acquisition: dict, needs: list[str] | None = None, *, menu=False) -> dict:
    if needs is None and acquisition.get("question_alternatives"):
        questions = acquisition["question_alternatives"]
        base = {k: v for k, v in acquisition.items() if k != "question_alternatives"} | {"question_origin": "policy"}
        attempts = []
        for question in questions:
            answer = route(plan, state, base | {"needs": question}, question)
            attempts.append({"needs": question, "status": answer.get("status", "selected")})
            if answer.get("status") != "blocked":
                return answer | {"question_attempts": attempts}
        return answer | {"question_attempts": attempts}
    if needs is not None and not acquisition.get("question_origin"):
        acquisition = acquisition | {"reason": "agent-requested-capability",
                                     "policy_reason": acquisition.get("reason"), "question_origin": "agent"}
    if acquisition["tool"] == "none" and not needs and not acquisition.get("requested_provider"):
        return acquisition
    needs = needs or acquisition.get("needs")
    if needs is None:
        if acquisition["tool"] == "timeline":
            needs = (["gpu-hotspot", "transfer", "sync"]
                     if acquisition["reason"] == "roi-wall-disagreement" else ["gpu-hotspot"])
            if requires_roi(plan):
                needs.append("roi-attribution")
        else:
            needs = (["register-pressure", "gpu-occupancy"]
                     if acquisition["reason"] == "static-resource-growth"
                     else ["memory-latency", "instruction-stalls"])
    aliases = {"gpu-kernel-time": "gpu-hotspot"}
    needs = list(dict.fromkeys(aliases.get(need, need) for need in needs))
    skills = load(REPO / "shared/config/registry.json", {}).get("skills", [])
    supported = {need for skill in skills for need in skill["answers"]}
    supported.update(need for spec in (plan.get("evidence_adapters") or {}).values()
                     for need in spec.get("capabilities", []))
    unknown = sorted(set(needs) - supported)
    if unknown:
        return acquisition | {"tool": "none", "status": "blocked", "needs": needs,
                              "reason": "unknown-capability", "unknown": unknown,
                              "supported": sorted(supported)}
    adapters = {"pw-nsight-systems": "timeline", "pw-nsight-compute": "kernel",
                "pw-cupti": "activity", "pw-hpctoolkit": "native", "pw-gpa": "native"}
    adapters |= {name: "custom" for name, spec in (plan.get("evidence_adapters") or {}).items()
                 if spec.get("probes") and spec.get("stages") and
                 set(needs).issubset(spec.get("capabilities") or [])}
    costs = state.get("acquisition_costs") or {}
    tools = (plan.get("profiling") or {}).get("tool_paths") or {}
    candidates, rejected = [], []
    run_dir = plan.get("run_dir")
    overflow = (run_dir and (requires_roi(plan) or "roi-attribution" in needs)
                and not acquisition.get("requested_provider")
                and load(REPO / Path(run_dir) / "baseline-acquisition.json", {}).get("fallback")
                == "roi-record-limit-exceeded")
    probe_plan = plan
    if limited(plan) and not acquisition.get("requested_provider"):
        probe_plan = plan | {"profiling": (plan.get("profiling") or {}) | {"probe_cache_only": True}}
    for skill in skills:
        name = skill["skill"]
        if acquisition.get("requested_provider") and name != acquisition["requested_provider"]:
            continue
        if not set(needs).issubset(skill["answers"]):
            continue
        # CPU cache/bandwidth and GPU value redundancy do not replace GPU counters.
        if (name == "pw-perf" or
                (name == "pw-gvprof" and not set(needs).issubset({"value-redundancy"})) or
                (name == "pw-drgpum" and not set(needs).issubset({"allocation", "gpu-memory-capacity"}))):
            continue
        native, activity = None, None
        pc_mode = name == "pw-cupti" and bool(set(needs) & {"gpu-pc", "instruction-stalls"})
        if name == "pw-cupti" and not pc_mode and overflow:
            rejected.append({"skill": name, "reason": "baseline-roi-record-limit-exceeded"})
            continue
        if pc_mode:
            from cupti_pc import CAPABILITIES, probe as pc_probe
            native = pc_probe(probe_plan) if set(needs) <= CAPABILITIES else {
                "status": "unavailable", "reason": "PC-sampler-does-not-provide-requested-timing-or-counters", "capabilities": []}
            installed = native["status"] == "available" and set(needs) <= set(native["capabilities"])
            checks = [native.get("reason") or "current-PC-sampling-probed"]
            adapters[name] = "pc-sampling"
        elif name in PROVIDERS and adapters.get(name) == "native":
            native = native_probe(name, probe_plan)
            installed = native["status"] == "available" and set(needs).issubset(native["capabilities"])
            checks = [native.get("reason") or "native-capabilities-probed"]
        else:
            installed, checks = available(skill, tool_paths=tools,
                                          artifacts=set((plan.get("profiling") or {}).get("artifacts") or []))
        if name == "pw-cupti" and installed and not pc_mode:
            from cupti_activity import collector, collector_status
            installed = not os.environ.get("CUDA_INJECTION64_PATH") and collector(plan) is not None
            if not installed:
                activity = ({"reason": "existing-cuda-injection-conflict"} if os.environ.get("CUDA_INJECTION64_PATH")
                            else collector_status())
        if not installed or name not in adapters:
            rejected.append({"skill": name, "reason": activity.get("reason", "activity-collector-unavailable") if activity else
                             (native.get("reason") or "requested-capability-not-advertised") if native else
                             "unavailable" if not installed else "adapter-unconfigured",
                             **({"probe": activity["artifact"]} if activity and activity.get("artifact") else {}),
                             **({"capabilities": native["capabilities"], "probe": native.get("probe_artifact") or native.get("artifact")}
                                if native else {})})
            continue
        pc = any(n in needs for n in ("gpu-source-line", "instruction-stalls", "memory-latency", "causal-stall-attribution"))
        mode = ("pc" if pc else "context") if adapters[name] == "native" else adapters[name]
        cost_key = name + ":" + mode
        if limited(plan):
            cost_key += ":" + ",".join(sorted(needs))
        legacy_cost = (costs.get(name) if adapters[name] not in {"native", "pc-sampling"} and
                       not (name == "pw-cupti" and costs.get("pw-cupti:pc-sampling")) else [])
        history = costs.get(cost_key) or ([] if limited(plan) else legacy_cost) or []
        measured = sum(history) / len(history) if history else None
        if limited(plan) and measured is not None and measured > remaining():
            rejected.append({"skill": name, "reason": "measured-cost-exceeds-acquisition-budget",
                             "measured_s": measured})
            continue
        candidates.append({"skill": name, "tool": adapters[name], "measured_s": measured,
                           "cost_rank": skill["cost_rank"], "cost_key": cost_key,
                           **({"opportunity": with_cost(acquisition["opportunity"], measured)}
                              if acquisition.get("opportunity") else {}),
                           **({"capability_probe": native.get("probe_artifact")} if native else {})})
    if menu:
        return {"candidates": candidates, "unavailable": rejected}
    if not candidates:
        return acquisition | {"tool": "none", "status": "blocked", "needs": needs,
                              "reason": "no-capable-installed-adapter", "unavailable": rejected}
    # Compare seconds only with seconds. An uncalibrated equivalent provider must
    # not make the router discard a measured cheap alternative. It remains
    # selectable for a distinctive capability or explicit calibration request.
    measured = [x for x in candidates if x["measured_s"] is not None]
    have_measurements = bool(measured)
    pool = measured or candidates
    pool.sort(key=lambda x: (x["measured_s"] if have_measurements else x["cost_rank"], x["skill"]))
    selected = pool[0]
    from skill_runtime import rank
    learned = rank(plan, state, acquisition, needs, candidates)
    if learned:
        selected = learned["candidate"]
    return acquisition | {"tool": selected["tool"], "provider": selected["skill"],
                          "needs": needs, "measured_cost_s": selected["measured_s"],
                          "cost_key": selected["cost_key"],
                          "cost_basis": "measured-wall" if have_measurements else "configured-prior",
                          **({"adaptation": {k: v for k, v in learned.items() if k != "candidate"}}
                             if learned else {}),
                          **({"opportunity": selected["opportunity"]} if selected.get("opportunity") else {}),
                          "alternatives": [x for x in candidates if x is not selected], "unavailable": rejected}
