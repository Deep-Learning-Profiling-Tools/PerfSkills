"""Resolve profiler facts produced by cached preflight and baseline discovery."""

from __future__ import annotations

import re
from pathlib import Path

from experiment_support import load
from start_contract import AUTO_KERNEL, selected_kernel


def _row(result: dict, check_id: str) -> dict:
    return next((item for item in result.get("checks", []) if item.get("id") == check_id), {})


def _sections(row: dict) -> list[str]:
    path = Path(row.get("stdout", ""))
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    names = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s{2,}", line)
        if match and match.group(1) not in {"Identifier"}:
            names.append(match.group(1))
    return sorted(set(names))


def hydrate(plan: dict, run: Path, baseline_profile: Path) -> None:
    """Fill internal fields; callers persist the resolved plan atomically."""
    kernel = plan.get("kernel_profile") or {}
    if not kernel.get("enabled"):
        return
    preflight = load(run / "preflight-result.json", {}) or {}
    version_row = _row(preflight, "ncu-version")
    version = (version_row.get("extracted") or {}).get("ncu_version")
    if version:
        kernel["tool_version"] = version
    sections = _sections(_row(preflight, "ncu-sections"))
    if sections:
        kernel["available_sections"] = sections
    auto_keys = [kernel.get(key) for key in ("baseline_kernel_regex", "variant_kernel_regex")]
    attribution = plan.get("attribution") or {}
    auto_keys += [attribution.get(key) for key in
                  ("baseline_target_regex", "variant_target_regex")]
    if AUTO_KERNEL not in auto_keys:
        return
    profile = load(baseline_profile, {}) or {}
    pattern = selected_kernel(profile)
    for key in ("baseline_kernel_regex", "variant_kernel_regex"):
        if kernel.get(key) == AUTO_KERNEL:
            kernel[key] = pattern
    for key in ("baseline_target_regex", "variant_target_regex"):
        if attribution.get(key) == AUTO_KERNEL:
            attribution[key] = pattern
