"""Read explicitly labeled wall-clock phases from a passing application sample."""

import math
import re
from pathlib import Path

_LABEL = re.compile(r"\s*([A-Za-z][\w ./():/-]{0,95}):\s*")
_DIVIDER = re.compile(r"\s*[=-]{5,}\s*")
_TIMER = re.compile(
    r"\s*wall(?:[- ]clock)? time\s*[=:]\s*"
    r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?:s|seconds?)\s*", re.I)


def parse(stdout):
    lines = stdout.splitlines()
    section = None
    phases = {}
    for index in range(1, len(lines)):
        line, previous = lines[index], lines[index - 1]
        label = _LABEL.fullmatch(line)
        if label and index + 1 < len(lines) and _DIVIDER.fullmatch(previous) and _DIVIDER.fullmatch(lines[index + 1]):
            section = label[1].strip()
        timer, heading = _TIMER.fullmatch(line), _LABEL.fullmatch(previous)
        if not (timer and heading):
            continue
        seconds = float(timer[1])
        if not math.isfinite(seconds) or seconds <= 0:
            continue
        name = heading[1].strip()
        key = (section or name, name)
        # Repeated labels may come from different ranks or phase invocations.
        phases[key] = None if key in phases else {"phase": key[0], "timer": name, "seconds": seconds}
    return sorted((row for row in phases.values() if row), key=lambda row: row["seconds"], reverse=True)[:8]


def from_sample(run, sample):
    if not (sample.get("oracle") or {}).get("passed"):
        return []
    if "application_phases" in sample:
        return sample["application_phases"]
    # Older baseline records retain the same passing sample's stdout.
    path = Path(sample.get("stdout") or "").resolve()
    if path.is_file() and path.is_relative_to(run.resolve()):
        with path.open(errors="replace") as stream:
            return parse(stream.read(131072))
    return []
