"""Measured runtime accounting, including cached retries and failed captures."""

import json
from pathlib import Path
from datetime import datetime, timezone


def sample_cost(rows):
    return {"executions": len(rows), "execution_s": sum(x.get("wall_s", 0) for x in rows),
            "oracle_s": sum(x.get("oracle_s", 0) for x in rows)}


def campaign_cost(builds: dict, candidates: list[dict], final: dict, phase: str, elapsed: float):
    screens = [] if phase == "final-only" else [x for row in candidates
        for kind in ("baseline", "candidate") for x in (row.get("samples") or {}).get(kind, []) if not x.get("reused")]
    warmups = [] if phase == "final-only" else [x for row in candidates for x in row.get("warmup_samples", [])]
    finals = [] if phase == "screen" else [x for rows in final.get("samples", {}).values() for x in rows]
    screen, warmup, confirm = (sample_cost(rows) for rows in (screens, warmups, finals))
    build = sum((x or {}).get("spent_s", 0) for x in builds.values())
    known = build + sum(x["execution_s"] + x["oracle_s"] for x in (screen, warmup, confirm))
    return {"build_s": build, "screening": screen, "warmup": warmup, "confirmation": confirm,
            "controller_and_statistics_s": max(0, elapsed - known), "elapsed_s": elapsed}


def event(run: Path, category: str, elapsed: float, **details):
    path = run / "raw/experiment/cost-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"category": category, "elapsed_s": elapsed,
           "utc": datetime.now(timezone.utc).isoformat(), **details}
    with path.open("a") as stream:
        stream.write(json.dumps(row, separators=(",", ":")) + "\n")
