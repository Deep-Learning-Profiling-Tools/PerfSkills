"""Reduce recorded controller wall times without counting cached work twice."""

import json
from pathlib import Path


def summarize(run: Path) -> dict:
    from resource_meter import totals
    resources = totals(run)
    path = run / "raw/experiment/cost-events.jsonl"
    if not path.exists():
        return {"status": "unavailable", "reason": "runtime-cost-events-not-recorded", "resources": resources}
    totals, count = {}, 0
    for line in path.read_text().splitlines():
        row = json.loads(line)
        key = row["category"]
        totals[key] = totals.get(key, 0) + row["elapsed_s"]
        count += 1
    return {"status": "available", "resources": resources, "scope": "recorded-controller-commands-excludes-agent-think-time",
            "categories_s": totals, "total_s": sum(totals.values()), "events": count,
            "artifact": str(path), "historical_missing_events": "not reconstructed"}
