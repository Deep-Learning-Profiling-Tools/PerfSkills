"""Reduce timestamped activity inside an observed NVTX range, never infer a range."""


def union(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def inside(intervals, windows):
    return [[max(a, c), min(b, d)] for a, b in intervals for c, d in windows
            if max(a, c) < min(b, d)]


def region(record, name, excluded=None):
    """First complete named range, with contained exclusion ranges subtracted."""
    if record.get("phase_records_truncated"):
        return None
    starts, ranges = {}, []
    for m in sorted(record.get("markers", []), key=lambda m: m["timestamp"]):
        key = (m.get("domain", ""), m["id"])
        if m["flags"] & 2:
            starts[key] = m
        if m["flags"] & 4 and key in starts:
            first = starts.pop(key)
            if 0 < first["timestamp"] < m["timestamp"]:
                ranges.append((first["name"], first["timestamp"], m["timestamp"]))
    chosen = sorted((a, b) for n, a, b in ranges if n == name)
    if not chosen:
        return None
    start, end = chosen[0]
    windows = [[start, end]]
    for lo, hi in union([(a, b) for n, a, b in ranges if n == (excluded or name + "-excluded")]):
        windows = [[a, min(b, lo)] for a, b in windows if a < lo] + [
                   [max(a, hi), b] for a, b in windows if b > hi]
    windows = union(windows)
    duration = sum(b - a for a, b in windows)
    if not duration:
        return None
    kernels, intervals = [], []
    for row in record.get("kernels", []):
        original = row.get("intervals_ns", [])
        clipped = inside(original, windows)
        if clipped:
            kernel = {k: v for k, v in row.items() if k != "intervals_ns"} | {
                "total_ns": sum(b - a for a, b in clipped),
                "calls": sum(bool(inside([i], windows)) for i in original)}
            # Geometry and interval zero describe the same recorded activity.
            first_start, first_end = original[0]
            if not any(a <= first_start < first_end <= b for a, b in windows):
                for field in ("first_grid", "first_block", "registers_per_thread"):
                    kernel.pop(field, None)
            kernels.append(kernel)
            intervals.extend(clipped)
    intervals += inside(record.get("copy_intervals_ns", []), windows)
    return {"kernels": sorted(kernels, key=lambda k: k["total_ns"], reverse=True),
            "wall_s": duration / 1e9,
            "gpu_busy_s": sum(b - a for a, b in union(intervals)) / 1e9,
            "scope": {"policy": "nvtx-roi", "name": name, "window_ns": [start, end],
                      "exclusions": excluded or name + "-excluded", "clock": "CUPTI-activity-timestamps",
                      "outside_roi": "collateral-only", "processes": 1}}
