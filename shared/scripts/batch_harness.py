#!/usr/bin/env python3
"""Run independent ProfileWeaver candidate plans as one quiet experiment batch.

Candidate plans execute sequentially because they commonly share one GPU. Full harness output
is saved per candidate; stdout receives one bounded result so a batch costs one agent exchange.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def resolve(value, base=REPO):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--max-agent-tokens", type=int, default=320)
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != 1 or not isinstance(manifest.get("plans"), list):
        raise SystemExit("batch manifest must have version=1 and a plans array")
    run = resolve(manifest.get("run_dir", f"runs/{manifest['run_id']}"))
    raw = run / "raw" / "batch"
    raw.mkdir(parents=True, exist_ok=True)
    results = []
    started_all = time.perf_counter()
    for index, entry in enumerate(manifest["plans"], 1):
        if isinstance(entry, str):
            entry = {"id": f"candidate-{index:02d}", "plan": entry}
        candidate_id = entry.get("id") or f"candidate-{index:02d}"
        plan = resolve(entry["plan"], manifest_path.parent)
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(REPO / "shared/scripts/harness.py"), str(plan)],
            cwd=REPO, text=True, capture_output=True,
        )
        elapsed = time.perf_counter() - started
        (raw / f"{candidate_id}.stdout.txt").write_text(proc.stdout)
        (raw / f"{candidate_id}.stderr.txt").write_text(proc.stderr)
        last = next((line for line in reversed(proc.stdout.splitlines()) if line.strip()), "")
        results.append({"id": candidate_id, "plan": str(plan), "returncode": proc.returncode,
                        "elapsed_s": elapsed, "last_status": last[:500]})
        if proc.returncode and (args.stop_on_failure or not entry.get("allow_failure", False)):
            break
    brief_proc = subprocess.run(
        [sys.executable, str(REPO / "shared/scripts/brief.py"), str(run),
         "--phase", manifest.get("brief_phase", "iterate"),
         "--max-tokens", str(args.max_agent_tokens)],
        cwd=REPO, text=True, capture_output=True,
    )
    (raw / "brief.stdout.txt").write_text(brief_proc.stdout)
    (raw / "brief.stderr.txt").write_text(brief_proc.stderr)
    ok = len(results) == len(manifest["plans"]) and all(item["returncode"] == 0 for item in results)
    payload = {
        "protocol": "profileweaver.batch-result/v1",
        "run_id": manifest["run_id"],
        "status": "complete" if ok else "partial-or-failed",
        "elapsed_s": time.perf_counter() - started_all,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "candidates": results,
        "agent_brief": str(run / "agent-brief.txt") if brief_proc.returncode == 0 else None,
        "brief_returncode": brief_proc.returncode,
    }
    output = run / "batch-result.json"
    output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"status": payload["status"], "run_id": manifest["run_id"],
                      "completed": len(results), "requested": len(manifest["plans"]),
                      "brief": payload["agent_brief"], "result": str(output)},
                     separators=(",", ":")))
    return 0 if ok and brief_proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
