"""Deliver fresh, bounded evidence inline, with named detail on disk."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from brief_facts import token_counter
from brief_current import current_evidence
from candidate_evidence import profile_for
from compiler_evidence import for_target
from kernel_identity import simple_base
from experiment_support import display, load, write
from terminal_view import terminal_ncu
from evidence_scope import requires_roi
from iteration_packet import emit_packet
from source_inspect import index_path


def _name(name, limit=105):
    if not name or len(name) <= limit:
        return name
    return name[:limit - 16] + "…#" + hashlib.sha256(name.encode()).hexdigest()[:12]


def build(plan: dict, run: Path, state: dict, capsule: dict, row: dict, *, confirmation=False,
          max_tokens=640, evidence_only=False, delivered=True) -> dict:
    candidate_id = capsule["id"]
    profile, path = profile_for(run, candidate_id)
    # A stored prior capture remains useful after deferred acquisition, but it is
    # always labeled as that snapshot's evidence, not a new capture.
    acquired = capsule.get("status") in {"complete", "partial"}
    if acquired and capsule.get("profile_artifact"):
        supplied = Path(capsule["profile_artifact"])
        supplied = supplied if supplied.is_absolute() else Path(__file__).resolve().parents[2] / supplied
        profile, path = load(supplied, profile), supplied
    ncu = capsule.get("ncu") or {}
    if ncu.get("status") not in {None, "complete"}:
        ncu = {}
    final = confirmation and (state.get("confirmed") or {}).get("id") == candidate_id
    if final:
        timeline = load(run / "profile.reprofile.json", {})
        if timeline.get("phase") == "reprofile" and timeline.get("variant") == candidate_id:
            profile = timeline
            ncu = (state.get("kernel_profiles") or {}).get("final") or {}
            acquired = True
    top = (profile.get("hotspots") or [{}])[0] if acquired else {}
    name = ncu.get("kernel") or top.get("name") or (capsule.get("next") or {}).get("name")
    anchor = current_evidence(run, state, candidate_id=candidate_id, require_roi=requires_roi(plan)) if not acquired else None
    static_name = name or (anchor or {}).get("name")
    resources = for_target(capsule.get("static") or load(run / "compiler" / (candidate_id + ".json"), {}) or {}, static_name)
    actual_resource = (resources.get("kernels") or [{}])[0]
    source_path = index_path(run, "final" if final else candidate_id)
    sources = load(source_path, {})
    targets = sources.get("targets") or []
    matching = next((t for t in targets if t.get("hotspot") == name), {})
    if not matching and resources.get("identity_match") == "unique-compiled-kernel-name":
        matching = next((t for t in targets if simple_base(t.get("hotspot")) == simple_base(name)), {})
    acquisition = capsule.get("acquisition") or {}
    improvement = row.get("improvement") or {}
    ranking = row.get("ranking") or {}
    view = {"id": candidate_id, "status": "complete" if final and acquired else capsule.get("status"),
            "roi": {"baseline": (row.get("baseline_metric") or {}).get("median"),
                    "candidate": (row.get("metric") or {}).get("median"),
                    "speedup": improvement.get("point"), "pairs": row.get("pairs"),
                    "correct": (row.get("correctness") or {}).get("passed"),
                    "role": "existing-timing-no-new-benchmark" if evidence_only else "unprofiled-ranking",
                    "provisional": not ((confirmation or evidence_only) and
                        (state.get("confirmed") or {}).get("id") == candidate_id),
                    "quality": ranking.get("status"),
                    "timing_disagreement": ranking.get("diagnostic_disagreement", False),
                    "instrumented_sample": ranking.get("profiled_metric")},
            "acquisition": {k: acquisition[k] for k in ("provider", "requested_provider", "reason", "policy_reason", "needs", "cost_basis", "status", "action", "historical_fact", "historical_context") if k in acquisition},
            "observations": {"snapshot": candidate_id, "scope": profile.get("scope") or capsule.get("scope"),
                             "target": _name(name)},
            "source": {"slice": (matching.get("slice") or {}).get("path"),
                       "targets": display(source_path)},
            "static": {"target": _name(resources.get("active_target")),
                       "execution_identity": "observed" if acquired else "historical-name-only",
                       **{k: actual_resource[k] for k in ("registers", "local_bytes", "stack_bytes", "shared_bytes") if k in actual_resource}},
            "unknowns": [], "detail": display(run / "candidates" / candidate_id / "evidence.json")}
    if acquisition.get("status") == "blocked":
        view["unknowns"] += [f"{item['skill']}: {item['reason']}" for item in acquisition.get("unavailable", [])[:2]]
    if row.get("measurement_role"):
        view["roi"]["measurement_role"] = row["measurement_role"]
    if row.get("runs") is not None:
        view["roi"].update({"runs": row["runs"], "baseline_reused": row.get("baseline_reused"),
                            "confidence": "measured-only"})
    if acquisition.get("opportunity"):
        score = acquisition["opportunity"]
        view["acquisition"]["opportunity"] = {k: score.get(k) for k in
            ("snapshot", "bound_basis", "roi_speedup_ceiling", "priority_per_profiler_second")}
    if acquisition.get("adaptation"):
        learned = acquisition["adaptation"]
        view["acquisition"]["learned"] = {k: learned[k] for k in ("reason", "revision")}
    if capsule.get("status") in {"blocked", "partial", "inconclusive"}:
        view["acquisition"]["unavailable_reason"] = capsule.get("reason")
        view["unknowns"] += capsule.get("unanswered", [])
    if ncu:
        counter = terminal_ncu(ncu, findings=True) or {}
        counter.pop("artifact", None)
        counter.pop("gpa_alternatives", None)
        counter["classification_source"] = "NCU-derived-rules"
        view["observations"]["counters"] = counter
        if final:
            actual = (profile.get("tool") or {}).get("name")
            timing_provider = {"cupti-activity": "pw-cupti", "nsight-systems": "pw-nsight-systems"}.get(actual, actual)
            view["acquisition"] = {"providers": [timing_provider, "pw-nsight-compute"],
                                   "reason": "independent-final-winner-evidence"}
    elif acquired and capsule.get("surface") == "pw-cupti":
        target = capsule.get("target") or {}
        view["observations"].update({"kernel_sum_ms": target["total_ns"] / 1e6 if target.get("total_ns") is not None else None,
                                    "calls": target.get("calls"), "launches": capsule.get("launches"), "activity_totals_scope": "whole-process",
                                    "copy_calls": capsule.get("copy_calls"), "copy_bytes": capsule.get("copy_bytes")})
        view["unknowns"].append("Activity does not establish cache, stalls or host synchronization." if
            isinstance(capsule.get("scope"), dict) and capsule["scope"].get("policy") == "nvtx-roi" else
            "Activity sums do not establish ROI wall, cache, stalls or host synchronization.")
    elif acquired and top:
        view["observations"]["kernel_time_s"] = top.get("self_time_s")
        view["observations"]["calls"] = top.get("calls")
    provider = capsule.get("provider_evidence") or profile.get("provider_evidence") or {}
    if provider:
        for key in ("findings", "contexts", "stalls", "locations", "operation_totals"):
            if provider.get(key):
                records = deepcopy(provider[key][:1 if key in {"findings", "contexts", "locations"} or (key == "stalls" and provider.get("locations")) else 3])
                for item in records:
                    if key == "locations" and item.get("metrics"):
                        metrics = item.pop("metrics")
                        metric, samples = max(((k, v) for k, v in metrics.items() if not k.endswith("_not_issued")), default=(None, None), key=lambda x: x[1] or 0)
                        item.update({"metric": metric, "samples": samples})
                        item["kernel"] = _name(item.get("kernel"))
                        item.pop("cubin_crc", None)
                    if "callpath" in item:
                        item["callpath"] = [_name(frame.get("name"), 72) for frame in item["callpath"]
                                            if isinstance(frame, dict) and frame.get("module")][-4:]
                        for redundant in ("context_id", "module", "name", "file", "line"):
                            if redundant in item and (redundant not in {"file", "line"} or item[redundant] is None):
                                item.pop(redundant, None)
                        if str(item.get("file", "")).startswith("["):
                            item.pop("file", None)
                    if "locations" in item:
                        item["locations"] = item["locations"][:2]
                view["observations"][key] = records
        if provider.get("operation_totals"):
            view["observations"].pop("operation_totals", None)
            view["observations"]["operations"] = {
                item["metric"]: {"value": float(f"{item['value']:.7g}"), "unit": item.get("unit")}
                for item in provider["operation_totals"][:10]}
        view["unknowns"] += provider.get("limitations", [])[:2]
    if not acquired:
        view["observations"] = {"status": "no-new-capture", "snapshot": None}
        if capsule.get("reference_evidence"):
            view["observations"]["reference_evidence"] = capsule["reference_evidence"]
        if anchor:
            view["observations"]["historical_anchor"] = {"candidate": anchor["id"],
                                                         "target": _name(anchor["name"])}
            view["source"] = {"snapshot": anchor["id"], "slice": anchor.get("slice"),
                               "targets": anchor.get("source_targets")}
        view["unknowns"].append("No new dynamic evidence for this evaluation.")
    if resources.get("active_target") is None:
        view["unknowns"].append("Static resources for the observed target are unresolved.")
    frontier = [{"id": t.get("id"), "relation": (t.get("dependency") or {}).get("relation", "hotspot"),
                 "parent": (t.get("dependency") or {}).get("parent")}
                for t in targets if (t.get("dependency") or {}).get("relation") in {"existing-variant", "writer", "callee", "transfer-or-allocation"}]
    if frontier:
        view["source"]["frontier"] = frontier[:4]
    view["drilldown"] = {"section": "evidence", "candidate": candidate_id,
                          "groups": ["resources", "routing"] +
                          [k if k != "operation_totals" else "timing" for k in
                           ("contexts", "stalls", "locations", "findings", "operation_totals") if provider.get(k)]}
    if (plan.get("source") or {}).get("baseline_dir") and candidate_id in state.get("candidates", {}):
        from source_observations import controls
        source_controls = controls(plan, run, state, candidate_id)
        if source_controls["controls"]:
            view["source"]["controls"] = source_controls["controls"][:2]
            view["source"]["control_scope"] = "lexical-only"
    if acquired:
        from evidence_views import groups
        view["drilldown"]["groups"] = sorted(set(view["drilldown"]["groups"]) | set(groups(profile, ncu)))
    from selection_objective import kernel_mode, summary
    if kernel_mode(state):
        measured = state["candidates"].get(candidate_id, {}).get("objective_result") or {}
        view["selection"] = summary(state) | {"result": {k: measured.get(k) for k in ("status", "seconds", "speedup")}}
        view["roi"]["role"] = "application-collateral"
    from inspection_summary import patch_regions
    view["source"]["changes"] = patch_regions(run, candidate_id)
    from iteration_context import complete
    complete(plan, run, state, candidate_id, view, source_path, targets, timeline=capsule.get("timeline"))
    from decision_learning import active, delivery
    from evidence_presentation import enabled as lookahead, prepare, record
    presentation, receipts = None, {}
    if lookahead(plan) and delivered:
        presentation, receipts = prepare(plan, run, state, candidate_id, view, acquisition.get("provider"), acquired)
    agent, tokens = emit_packet(run, candidate_id, view, profile, path, max_tokens,
                                compact=active(plan), presentation=presentation)
    if receipts:
        record(plan, run, agent, receipts, tokens)
    if active(plan) and delivered:
        delivery(plan, run, state, capsule, agent, tokens)
    return agent
