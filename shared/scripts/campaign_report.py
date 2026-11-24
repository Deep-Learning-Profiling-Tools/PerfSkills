"""Render the complete saved iteration ledger without running diffs, profilers or token audits."""

import json
from pathlib import Path
from change_manifest import build as changes
from experiment_support import load, sha256, write
from resource_meter import totals
from skill_binding import digest
from screen_outcome import describe


FORMAT_REVISION = 3


def text(value):
    return str(value).replace("|", "/").replace("\n", " ")


def ranges(edits, key):
    return ", ".join(str(start) if count == 1 else f"{start}–{start+count-1}" if count else
                     f"before {start}" for start,count in (e[key] for e in edits))


def render(run, verdict, rows):
    lines = ["# Optimization results", "", f"Selected source is {verdict.get('source')}.",
             f"Winner is {verdict.get('winner')}. Statistical significance is {verdict.get('statistical_significance', 'not-established')}.", "",
             "| Version | Parent | Mechanism | Primary speedup | Correct | Runs | Outcome |",
             "|---|---|---|---:|---|---:|---|"]
    for row in rows:
        lines.append("| " + " | ".join(text(row.get(k)) for k in
                     ("id","parent","mechanism","speedup","correct","executions","outcome")) + " |")
    for row in rows:
        lines += ["", f"## {row['id']}", "", f"Execution uses the declared command and environment. Parent correctness is {row['parent_correct']}.",
                  "", "| File | Functions in candidate | Parent lines | Candidate lines |",
                  "|---|---|---|---|"]
        for file in row["changes"].get("files", []):
            lines.append("| " + " | ".join(text(v) for v in
                (file["file"], ", ".join(file["functions_candidate"]) or "unresolved or file scope",
                 ranges(file["edits"],"parent"), ranges(file["edits"],"candidate"))) + " |")
        lines += ["", f"Exact literal edits and patch are saved in candidates/{row['id']}/source-changes.json."]
    return "\n".join(lines) + "\n"


def build(plan, run, state, verdict):
    signatures = []
    for cid, meta in state.get("candidates", {}).items():
        patch = run / "candidates" / cid / "change.patch"
        signatures.append([cid, meta.get("measurement_fingerprint"), sha256(patch) if patch.is_file() else None,
                           meta.get("objective_result"), meta.get("result")])
    signature = digest([FORMAT_REVISION, signatures, verdict, plan.get("execution"), plan.get("env"), state.get("selection_objective")])
    path = run / "iteration-ledger.json"
    prior = load(path, {})
    if prior.get("signature") == signature:
        resources = run / "resource-summary.json"
        usage = run / "session-usage.json"
        if usage.is_file() and (not resources.is_file() or usage.stat().st_mtime_ns > resources.stat().st_mtime_ns):
            saved = load(resources, {})
            saved["session_tokens"] = load(usage, {}).get("session_tokens")
            write(resources, saved)
        return prior
    rows = []
    for cid, meta in state.get("candidates", {}).items():
        row = meta.get("result") or {}
        primary = meta.get("objective_result") or {}
        kernel = state.get("selection_objective", {}).get("scope") == "kernel"
        parent = (state["candidates"].get(meta["parent"]) or {}).get("result") or {}
        rows.append({"id": cid, "parent": meta["parent"], "mechanism": meta.get("mechanism"),
                     "speedup": primary.get("speedup") if kernel else (row.get("improvement") or {}).get("point"),
                     "correct": (row.get("correctness") or {}).get("passed"), "outcome": describe(plan, state, cid)["status"],
                     "parent_correct": True if meta["parent"] == "baseline" else (parent.get("correctness") or {}).get("passed"),
                     "executions": row.get("runs"), "unprofiled_executions": row.get("benchmark_executions",row.get("runs")),
                     "profiled_executions":row.get("profiled_executions"),
                     "primary": primary if kernel else {"metric": row.get("metric"), "improvement": row.get("improvement")},
                     "application": {"metric": row.get("metric"), "improvement": row.get("improvement"),
                                     "measurement_role": row.get("measurement_role", "unprofiled")},
                     "changes": changes(plan, run, state, cid)})
    write(run / "resource-summary.json", totals(run))
    result = {"protocol": "profileweaver.iteration-ledger/v1", "format_revision": FORMAT_REVISION, "signature": signature, "complete": all(r["changes"].get("status")=="complete" for r in rows),
              "rows": rows, "verdict": verdict, "execution": plan.get("execution"), "environment": plan.get("env")}
    write(path, result)
    (run / "ITERATIONS.md").write_text(render(run, verdict, rows))
    return result


def compact(plan, run, state, verdict):
    ledger = build(plan, run, state, verdict)
    result = {k:verdict[k] for k in ("status","verification","campaign_complete","winner","source","objective",
               "improvement","correctness","statistical_significance","new_benchmark_runs","runs",
               "benchmark_executions","cached","new_comparison_runs") if k in verdict}
    reference = verdict.get("reference_comparison") or {}
    if reference:
        result["reference_comparison"] = {"provider": reference.get("provider"), "speedup": reference.get("speedup"),
            "baseline_s": (reference.get("baseline") or {}).get("seconds"),
            "candidate_s": (reference.get("candidate") or {}).get("seconds"), "status": reference.get("status")}
    result["report"] = {"complete": ledger["complete"], "iterations": len(ledger["rows"]), "markdown": str(run/"ITERATIONS.md"),
                        "json": str(run/"iteration-ledger.json"), "exact_changes": "candidates/<id>/source-changes.json"}
    result["resource_summary"] = str(run/"resource-summary.json")
    result["next_action"] = "report-saved-results"
    return result


def existing(plan, run):
    state = load(run / "experiment-state.json", {})
    verdict = load(run / "measurement-verdict.json", {}) or load(run / "final-verdict.json", {})
    if not verdict:
        raise ValueError("no final verdict is saved")
    return compact(plan, run, state, verdict)
