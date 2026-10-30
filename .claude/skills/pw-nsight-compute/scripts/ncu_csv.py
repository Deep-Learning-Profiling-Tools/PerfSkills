#!/usr/bin/env python3
"""Read and normalize raw Nsight Compute CSV metrics."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path


def load(path: Path | None, default=None):
    if not path:
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def key(row: dict, wanted: str):
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    return normalized.get(re.sub(r"[^a-z0-9]", "", wanted.lower()))


def number(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "nan", "inf", "-inf"}:
        return None
    scale = 1.0
    suffixes = {"k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}
    match = re.fullmatch(r"([-+0-9.eE]+)\s*([kKmMgGtT]?)", text)
    if not match:
        return None
    if match.group(2):
        scale = suffixes[match.group(2).lower()]
    try:
        value = float(match.group(1)) * scale
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def read_metrics(path: Path):
    lines = path.read_text(errors="replace").splitlines()
    # NCU sometimes prefixes diagnostics before its CSV header. The raw page is wide in current
    # releases (one column per metric); older/synthetic exports can be tall.
    start = next((i for i, line in enumerate(lines) if "Kernel Name" in line and
                  ("Metric Name" in line or "Process ID" in line)), None)
    if start is None:
        raise ValueError(f"no NCU raw metric header in {path}")
    parsed = list(csv.reader(lines[start:]))
    header = parsed[0]
    rows = []
    if "Metric Name" in header and "Metric Value" in header:
        for source in csv.DictReader(lines[start:]):
            metric = key(source, "Metric Name")
            if metric:
                rows.append({"kernel": key(source, "Kernel Name") or "unknown-kernel",
                             "metric": str(metric), "unit": key(source, "Metric Unit"),
                             "value": number(key(source, "Metric Value")),
                             "value_raw": key(source, "Metric Value")})
        return rows

    units = parsed[1] if len(parsed) > 1 else [""] * len(header)
    kernel_index = header.index("Kernel Name")
    identity = {"ID", "Process ID", "Process Name", "Host Name", "Kernel Name", "Context",
                "Stream", "Block Size", "Grid Size", "Device", "CC"}
    for record in parsed[2:]:
        if len(record) != len(header):
            continue
        kernel = record[kernel_index] or "unknown-kernel"
        for index, metric in enumerate(header):
            if metric in identity:
                continue
            value = number(record[index])
            if value is None:
                continue
            rows.append({"kernel": kernel, "metric": metric, "unit": units[index] or None,
                         "value": value, "value_raw": record[index]})
    return rows


def find_metric(metrics: dict, *names: str):
    for name in names:
        item = metrics.get(name)
        if item and item.get("value") is not None:
            return item
    return None


def contains_metric(metrics: dict, *parts: str):
    matches = [item for name, item in metrics.items()
               if all(part in name for part in parts) and item.get("value") is not None]
    return max(matches, key=lambda item: abs(item["value"])) if matches else None


def field(item):
    if not item:
        return None
    return {"value": item["value"], "unit": item.get("unit"), "raw_metric": item["metric"]}


def compact_non_null(value):
    if isinstance(value, dict):
        return {k: compact_non_null(v) for k, v in value.items() if v is not None}
    return value


def metric_priority(item: dict):
    name = item["metric"]
    score = 0
    if name.endswith((".pct", ".ratio", ".sum", ".avg")):
        score += 4
    if "pct_of_peak_sustained" in name or "per_cycle_active" in name:
        score += 4
    if not name.startswith("breakdown:"):
        score += 2
    if any(text in name for text in ("peak_sustained", "max_rate", "device__attribute")):
        score -= 2
    return (-score, name)
