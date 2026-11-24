"""Answer evidence questions from an already captured and validated baseline timeline."""

from evidence_scope import requires_roi, roi_scope
from experiment_support import load
from profile_observations import timeline_context, timeline_observations


def timeline(plan, run, state, needs, provider):
    if not needs or provider not in {None, "pw-nsight-systems"}:
        return None
    identity = state.get("contract_sha256")
    if not identity or load(run / "baseline-fingerprint.json", {}).get("contract_sha256") != identity:
        return None
    path = run / "profile.baseline.json"
    profile = load(path, {})
    if profile.get("phase") != "baseline" or (requires_roi(plan) and not roi_scope(profile.get("scope"))):
        return None
    details = load(run / "roi-metrics.json", {})
    caps = timeline_observations(profile, details)
    if not set(needs).issubset(caps):
        return None
    facts = timeline_context(details, needs)
    if "sync" in needs and "sync_calls" not in facts:
        return None
    return {"id": "baseline", "status": "complete", "cache": "hit", "profile_artifact": str(path),
            "scope": profile.get("scope"), "observed_capabilities": caps, "timeline": facts,
            "acquisition": {"provider": "pw-nsight-systems", "needs": needs, "reason": "baseline-timeline-reused"}}
