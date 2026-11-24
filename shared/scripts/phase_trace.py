"""Find the preceding CUDA operation on each host thread in a saved ROI trace."""

from collections import Counter
from contextlib import closing
from pathlib import Path
import sqlite3


def predecessors(trace, window, operation):
    low, high = window
    if not trace or low is None or high is None or high <= low:
        return []
    try:
        with closing(sqlite3.connect(Path(trace).as_uri() + "?mode=ro", uri=True)) as con:
            names = dict(con.execute("SELECT id, value FROM StringIds"))
            launch_ids = {key for key, name in names.items() if name.startswith(
                ("cudaLaunchKernel", "cudaLaunchCooperativeKernel"))}
            targets = {key for key, name in names.items() if name == operation}
            if not targets:
                return []
            tracked = sorted(launch_ids | targets | {key for key, name in names.items() if name.startswith("cudaMemcpy")})
            kernels = {}
            for cid, name in con.execute("SELECT correlationId, shortName FROM CUPTI_ACTIVITY_KIND_KERNEL "
                                         "WHERE start < ? AND end > ?", (high, low)):
                # A graph or overlapping process can make the launch symbol ambiguous.
                if kernels.setdefault(cid, name) != name:
                    kernels[cid] = None
            placeholders = ", ".join("?" for _ in tracked)
            rows = con.execute("SELECT nameId, globalTid, correlationId FROM CUPTI_ACTIVITY_KIND_RUNTIME "
                               f"WHERE start >= ? AND start < ? AND nameId IN ({placeholders}) "
                               "ORDER BY start, correlationId", (low, high, *tracked))
            last, counts = {}, Counter()
            for name_id, thread, correlation in rows:
                if name_id in targets and thread in last:
                    counts[last[thread]] += 1
                kernel = names.get(kernels.get(correlation)) if name_id in launch_ids else None
                last[thread] = (kernel or names[name_id], "kernel-launch" if kernel else "runtime-api")
    except sqlite3.Error:
        return []
    return [{"operation": name, "kind": kind, "calls": calls} for (name, kind), calls in counts.most_common(5)]
