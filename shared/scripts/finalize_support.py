"""Evidence and reporting helpers for campaign finalization."""

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")

def gate(passed, detail):
    return {"passed": bool(passed), "detail": detail}

def match_hotspot(profile, pattern):
    if not profile or not pattern:
        return None
    regex = re.compile(pattern)
    return next((item for item in profile.get("hotspots", [])
                 if regex.search(str(item.get("name", "")))), None)

def target_measurement(plan, before, after):
    spec = plan.get("attribution") or {}
    metric = spec.get("metric", "self_time_s")
    direction = spec.get("direction", "lower")
    left = match_hotspot(before, spec.get("baseline_target_regex"))
    right = match_hotspot(after, spec.get("variant_target_regex"))
    base = left.get(metric) if left else None
    opt = right.get(metric) if right else None
    moved = (base is not None and opt is not None and
             ((opt < base) if direction == "lower" else (opt > base)))
    return {"baseline": left, "variant": right, "metric": metric, "direction": direction,
            "base": base, "opt": opt, "moved": moved}

def limitations(profile):
    value = profile.get("limitations")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, dict):
        items = value.get("cannot_observe") or value.get("blind_spots") or []
        return [str(item) for item in items]
    return [str(value)]

def run_quiet(argv, raw_dir, name):
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{name}.stdout.txt").write_text(proc.stdout)
    (raw_dir / f"{name}.stderr.txt").write_text(proc.stderr)
    return proc.returncode

def outcome(item, winner, threshold):
    if not item["correctness"]["passed"]:
        return "incorrect"
    if item["id"] == winner:
        return "winner"
    comparison = item.get("improvement") or {}
    ci = comparison.get("ci95") or [0, 0]
    if ci[1] < 1 / threshold:
        return "regression"
    if ci[0] >= threshold:
        return "rejected"
    return "no-effect"
