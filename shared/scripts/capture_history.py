"""Keep complementary normalized captures for each measured snapshot outside profiler scratch.

The batch fingerprint identifies the measured inputs. The live snapshot is validated by
the caller before reuse for a verdict. Inspection can always read historical evidence.
"""

import hashlib
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

from experiment_support import REPO, load, sha256, write
from evidence_scope import roi_scope


def folder(run, candidate):
    return run / "candidates" / candidate / "observations"


def fingerprint(run, entry):
    path = run / "batches" / entry["id"] / "campaign-result.json"
    return (load(path, {}) or {}).get("fingerprint_after")


def preserve(run, candidate, capsule, identity):
    if (not identity or capsule.get("status") not in {"complete", "partial"} or capsule.get("cache") == "hit"
            or (capsule.get("ncu") or {}).get("disposition") == "reused"):
        return
    directory = run / "candidates" / candidate / "profile"
    source = REPO / (capsule.get("profile_artifact") or directory / "profile.json")
    if not source.is_file():
        return
    contents = source.read_bytes()
    profile = json.loads(contents)
    baseline = candidate == "baseline" and profile.get("phase") == "baseline"
    if not baseline and (profile.get("phase") != "variant" or profile.get("variant") != candidate):
        return
    root = folder(run, candidate)
    root.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256(contents).hexdigest()
    artifact = root / (checksum + ".json")
    if not artifact.exists():
        with NamedTemporaryFile(dir=root, delete=False) as stream:
            stream.write(contents)
        Path(stream.name).replace(artifact)
    entry = {"fingerprint": identity, "sha256": checksum, "tool": (profile.get("tool") or {}).get("name"),
             "scope": profile.get("scope"), "complete": capsule["status"] == "complete",
             "needs": (capsule.get("acquisition") or {}).get("needs"), "snapshot": candidate}
    from skill_experience import observed
    entry["capabilities"] = sorted(observed(capsule))
    entry["binding"] = capsule.get("capture_binding")
    entry["provider"] = (capsule.get("acquisition") or {}).get("provider")
    entry["target"] = (capsule.get("ncu") or {}).get("kernel") or (capsule.get("target") or {}).get("name")
    entry["summary"] = {k: capsule[k] for k in ("surface", "target", "next", "launches", "copy_calls", "timeline",
                      "copy_bytes", "observed_capabilities") if k in capsule}
    write(root / "index" / f"{time.time_ns()}-{checksum[:12]}.json", entry)
    capsule["profile_artifact"] = str(artifact)


def records(run, candidate, identity=None):
    root = folder(run, candidate)
    for record in sorted((root / "index").glob("*.json"), reverse=True):
        entry = load(record, {})
        if entry.get("snapshot") != candidate or (identity and entry.get("fingerprint") != identity):
            continue
        checksum = entry["sha256"]
        if not entry.get("complete"):
            continue
        path = root / (checksum + ".json")
        if path.is_file() and sha256(path) == checksum:
            yield entry, path


def captures(run, candidate, identity=None):
    seen = set()
    for _, path in records(run, candidate, identity):
        if path not in seen:
            seen.add(path)
            yield load(path, {}), path


def reuse(run, candidate, identity, binding_key, needs, provider, target, need_roi):
    """Reuse only observed facts on this measured binary, target and environment."""
    from kernel_evidence import compact_ncu
    from kernel_identity import same_target
    resources = load(run / "compiler" / (candidate+".json"), {})
    required = {"gpu-hotspot" if need == "gpu-kernel-time" else need for need in needs}
    for entry, path in records(run, candidate, identity):
        if (entry.get("binding") != binding_key or (provider and entry.get("provider") != provider)
                or not required.issubset(entry.get("capabilities") or [])):
            continue
        timing = required <= {"gpu-hotspot", "roi-attribution", "transfer", "launch-overhead", "sync"}
        if (timing and need_roi and not roi_scope(entry.get("scope"))) or (
                not timing and (not target or not same_target(resources,target,entry.get("target")))):
            continue
        profile = load(path, {})
        return entry["summary"] | {"id": candidate, "status": "complete", "cache": "hit",
                "profile_artifact": str(path), "scope": profile.get("scope"), "ncu": compact_ncu(profile),
                "acquisition": {"provider": entry["provider"], "needs": needs, "reason": "snapshot-evidence-reused"}}


def has_activity(profile, need_roi):
    return ((profile.get("totals") or {}).get("gpu_busy_s") is not None and
            (not need_roi or roi_scope(profile.get("scope"))) and
            not (profile.get("kernel_diagnostics") or {}).get("kernels"))


def timing_capture(run, candidate, entry, need_roi, binding_key=None):
    identity = fingerprint(run, entry) if entry else None
    if identity:
        for item, path in records(run, candidate, identity):
            if binding_key and binding_key != item.get("binding"):
                continue
            profile = load(path, {})
            if has_activity(profile, need_roi):
                return profile, path

def known_capabilities(run, state, snapshot):
    from skill_experience import observed
    meta = (state.get("candidates") or {}).get(snapshot) or {}
    current = meta.get("profile") or (state.get("baseline_evidence") if snapshot == "baseline" else {}) or {}
    known = observed(current) if current.get("status") == "complete" else set()
    if snapshot == "baseline":
        known.update(observed({"ncu": (state.get("kernel_profiles") or {}).get("baseline") or {}}))
    identity = meta.get("measurement_fingerprint") or (state.get("contract_sha256") if snapshot == "baseline" else None)
    target = (current.get("ncu") or {}).get("kernel") or (current.get("target") or {}).get("name")
    if identity:
        from kernel_identity import same_target
        resources = load(run / "compiler" / (snapshot+".json"), {})
        for index in (folder(run, snapshot) / "index").glob("*.json"):
            view = load(index, {})
            if view.get("complete") and view.get("fingerprint") == identity and target and same_target(resources,target,view.get("target")):
                known.update(view.get("capabilities") or [])
    return known


