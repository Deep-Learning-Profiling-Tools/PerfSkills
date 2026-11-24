"""Optional build/validation checkpoint before spending paired measurements."""

from __future__ import annotations

from pathlib import Path

from adaptive_policy import enabled
from candidate_workspace import item, make_patch
from campaign_run import run_build, run_command
from compiler_evidence import collect, for_target
from brief_current import current_evidence
from iteration_evidence import _name
from brief_facts import token_counter
import json
from experiment_state import save_state
from experiment_support import candidate_dir, write


def check(plan: dict, run: Path, state: dict, baseline: Path,
          candidate_id: str, build_only: bool = False, observe: bool = False) -> dict:
    if not enabled(plan):
        raise ValueError("check requires the adaptive policy")
    if build_only and observe:
        raise ValueError("--observe executes the workload and cannot be combined with --build-only")
    meta = state["candidates"].get(candidate_id)
    if not meta or meta.get("result"):
        raise ValueError("check requires a prepared, unmeasured candidate")
    cursor, depth = meta, 0
    while cursor and not cursor.get("result"):
        depth += 1
        cursor = state["candidates"].get(cursor.get("parent"))
    if depth > 2:
        raise ValueError("measure the deferred chain after at most two unmeasured candidates")
    workspace = Path(meta["workspace"])
    parent = baseline if meta["parent"] == "baseline" else Path(state["candidates"][meta["parent"]]["workspace"])
    patch = candidate_dir(run, candidate_id) / "change.patch"
    if not make_patch(parent, workspace, plan["source"]["fingerprint_globs"], patch):
        raise ValueError("candidate has no source change")
    spec = item(plan, workspace, candidate_id, patch)
    variables = {"repo": str(Path(__file__).resolve().parents[2]),
                 "run_dir": str(run), "run_id": plan["run_id"]}
    build = run_build(spec, plan, run, variables)
    collect(plan, run, baseline, "baseline")
    resources = collect(plan, run, workspace, candidate_id, meta["parent"])
    observation = None
    if observe:
        from cupti_activity import capture
        observation = capture(plan, run, candidate_id, workspace)
        validation = None
        correct = observation.get("correct")
    else:
        validation = None if build_only else run_command(
            spec, f"check-{candidate_id}", 0, plan, run, variables)
        correct = (validation or {}).get("oracle", {}).get("passed")
    status = "built" if build_only else "checked" if correct else "incorrect" if correct is False else "inconclusive"
    result = {"id": candidate_id, "status": status,
              "correct": correct, "timed_pairs": 0, "speedup": None,
              "verified": False, "deferred_depth": depth, "static": resources,
              "observation": observation,
              "build": build, "validation": validation}
    write(candidate_dir(run, candidate_id) / "check.json", result)
    meta["check"] = {key: result[key] for key in
                     ("status", "correct", "timed_pairs", "speedup", "verified", "deferred_depth")}
    meta["status"] = result["status"]
    save_state(run, state)
    anchor = current_evidence(run, state, candidate_id=candidate_id) or {}
    target = ((observation or {}).get("target") or {}).get("name") or anchor.get("name")
    selected = for_target(resources, target)
    resource = (selected.get("kernels") or [{}])[0]
    # The complete inventory is already stored in compiler/<ID>.json and check.json.
    # Do not send CUB templates and inactive variants through every checkpoint.
    compact = {key: value for key, value in result.items() if key not in
               {"build", "validation", "static", "observation"}}
    compact["static"] = {"status": resources.get("status"), "target": target,
                         "identity": "observed" if observation and observation.get("status") == "complete" and target else "historical-anchor-only",
                         **{k: resource[k] for k in ("registers", "local_bytes", "stack_bytes", "shared_bytes") if k in resource},
                         "artifact": resources.get("artifact")}
    if observation:
        compact["observation"] = {"status": observation.get("status"), "target": target,
                                  "scope": observation.get("scope"), "launches": observation.get("launches"),
                                  "kernel_sum_ms": ((observation.get("target") or {})["total_ns"] / 1e6
                                                    if (observation.get("target") or {}).get("total_ns") is not None else None),
                                  "artifact": observation.get("profile_artifact"), "role": "diagnostic-only"}
    compact["detail"] = str(candidate_dir(run, candidate_id) / "check.json")
    compact["static"]["target"] = _name(target)
    if compact.get("observation"):
        compact["observation"]["target"] = _name(target)
    count, method = token_counter()
    if count(json.dumps(compact, separators=(",", ":"))) > 600:
        compact = {k: compact[k] for k in ("id", "status", "correct", "timed_pairs", "speedup", "verified", "detail")}
        compact["observations"] = "Read named detail; checkpoint evidence exceeds inline budget."
    compact["context"] = {"max_tokens": 640, "method": method}
    compact["context"]["tokens"] = 0
    for _ in range(5):
        tokens = count(json.dumps(compact, separators=(",", ":")))
        if tokens == compact["context"]["tokens"]:
            break
        compact["context"]["tokens"] = tokens
    return compact
