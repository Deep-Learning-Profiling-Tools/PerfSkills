"""Normalize HPCToolkit context metrics and literal GPA instruction evidence.

No sampled time, summed GPU operation time, or advisor prediction is substituted
for application ROI, GPU busy union, or an unprofiled speedup.
"""

from __future__ import annotations

from native_gpa_reduce import reduce_gpa

from collections import defaultdict
import importlib.util
import math
from pathlib import Path
import re
import struct
import shutil
import subprocess

from experiment_support import REPO


def _string(data: bytes, position: int) -> str:
    if not position:
        return ""
    return data[position:data.index(b"\0", position)].decode("utf-8", "replace")


def read_v4(database: Path):
    # hpcanalysis reads the installed database format. Its default CCT reader
    # selects only the main thread and truncates C++ signatures. Retain all entry
    # points and literal function names in this adapter, without patching it.
    from hpcanalysis.data_read import DataRead

    class Reader(DataRead):
        def _parse_function(self, data, pointer):
            if pointer not in self._functions:
                name, module, offset, source, line = struct.unpack_from("<QQQQL", data, pointer)
                row = {"id": pointer, "name": _string(data, name), "line": line, "offset": offset}
                if source:
                    row["file_id"] = self._parse_source_file(data, source)["id"]
                if module:
                    row["module_id"] = self._parse_load_module(data, module)["id"]
                self._functions[pointer] = row
            return self._functions[pointer]

    reader = Reader(str(database))
    specs = reader.read_metric_descriptions()
    meta = (database / "meta.db").read_bytes()
    if meta[10:14] != b"meta":
        raise ValueError("unsupported HPCToolkit database format")
    _, context_pointer = struct.unpack_from("<QQ", meta, 16 + 6 * 8)
    entries, count, stride = struct.unpack_from("<QHB", meta, context_pointer)
    for index in range(count):
        size, children, context_id, entry_type = struct.unpack_from("<QQLH", meta, entries + index * stride)
        node = {"id": context_id, "type": "entry", "name": entry_type, "children": [], "depth": 0}
        reader._context.append(node)
        reader._parse_context(children, size, node, meta)
    # Ask for recorded values, not every declared metric id: sparse profiles omit
    # ids and older hpcanalysis versions cannot binary-search an absent final id.
    rows = reader.read_profile_slices([0], [], [])[0]
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    return (reader._context, list(reader._functions.values()), list(reader._source_files.values()),
            list(reader._load_modules.values()), specs, rows)


def reduce_contexts(contexts, functions, files, modules, specs, values) -> dict:
    nodes = {n["id"]: n for n in contexts}
    functions = {n["id"]: n for n in functions}
    files = {n["id"]: n.get("file_path") for n in files}
    modules = {n["id"]: n.get("module_path") for n in modules}
    metrics = defaultdict(dict)
    for row in values:
        value = row.get("value")
        if isinstance(value, (int, float)) and math.isfinite(value):
            metrics[row["cct_id"]][row["metric_id"]] = value
    # Select measured kernel seconds, favoring point/exclusive over inclusive
    # scope. Summed inclusive ancestors must not multiply a kernel's cost.
    kernel_specs = [s for s in specs if s.get("name", "").strip().lower() in
                    {"gker", "gpu time: kernel execution", "gpu kernel time"} and
                    s.get("unit") in {"s", "sec", "seconds"}]
    kernel_specs.sort(key=lambda s: (s.get("aggregation") != "sum",
                                    {"p": 0, "e": 1, "i": 2}.get(s.get("scope"), 3)))
    chosen = next((s for s in kernel_specs if any(s["id"] in v for v in metrics.values())), None)
    if chosen is None:
        raise ValueError("database has no recognized measured GPU kernel-seconds metric")
    metric_id = chosen["id"]

    def frame(node):
        function = functions.get(node.get("name"), {}) if node["type"] == "function" else {}
        source = files.get(node.get("file_path", function.get("file_id")))
        module = modules.get(node.get("module_path", function.get("module_id")))
        name = function.get("name") or (f"{source}:{node.get('line')}" if source else
                                        f"{module or 'context'}@{node.get('offset', node['id'])}")
        if source and source.startswith("["):
            source = None
        return {"name": name, "file": source, "line": node.get("line") or function.get("line") or None,
                "module": module}

    records, grouped = [], {}
    for node in contexts:
        own = metrics[node["id"]].get(metric_id, 0)
        if chosen.get("scope") == "i":
            own -= sum(metrics[child].get(metric_id, 0) for child in node.get("children", []))
        if own <= 1e-15:
            continue
        chain, current = [], node
        while current and current["type"] != "entry":
            chain.append(frame(current))
            current = nodes.get(current.get("parent"))
        owner = next((n for n in chain if n.get("module") and any(
            x in n["module"].lower() for x in (".cubin", ".gpubin", ".hsaco", "<gpu"))), None)
        target = owner or (chain[0] if chain else frame(node))
        key = (target["name"], target.get("file"), target.get("line"), target.get("module"))
        kind = "gpu-kernel" if owner else "unknown"
        hotspot = grouped.setdefault(key, {**target, "kind": kind, "self_time_s": 0,
            "pct_of_wall": None, "total_time_s": None, "calls": None,
            "callpath": [x["name"] for x in reversed(chain[-8:])], "metrics": {}})
        hotspot["self_time_s"] += own
        records.append({"context_id": node["id"], **frame(node), "kernel_time_s": own,
                        "callpath": list(reversed(chain[-8:]))})
    hotspots = sorted(grouped.values(), key=lambda h: h["self_time_s"], reverse=True)
    for rank, hotspot in enumerate(hotspots, 1):
        hotspot["rank"] = rank
    # Root inclusive GPU metrics carry operation sums, counts or bytes according
    # to their recorded units. These are not a union of overlapping intervals.
    roots = [n["id"] for n in contexts if n["type"] == "entry"]
    stalls = []
    for spec in specs:
        if not any(token in spec.get("name", "").lower() for token in ("stall", "gins:lat_")):
            continue
        observed = [(key, row[spec["id"]]) for key, row in metrics.items() if spec["id"] in row and row[spec["id"]] > 0]
        for context_id, value in sorted(observed, key=lambda pair: pair[1], reverse=True)[:2]:
            if context_id in nodes:
                stalls.append({"metric": spec["name"], "value": value, "unit": spec.get("unit"),
                               "scope": spec.get("scope"), "source": frame(nodes[context_id])})
    totals = []
    seen = set()
    for spec in sorted(specs, key=lambda s: s.get("aggregation") != "sum"):
        name = spec.get("name", "")
        if name not in {"gker", "gker:count", "gpuop", "gxcopy", "gxcopy:count", "gxcopy:h2d", "gxcopy:d2h", "gicopy", "gsync", "gsync:count"} or spec.get("scope") != "i" or name in seen:
            continue
        samples = [metrics[k][spec["id"]] for k in roots if spec["id"] in metrics[k]]
        if samples:
            totals.append({"metric": name, "value": sum(samples), "unit": spec.get("unit"),
                           "scope": "sum-over-profiled-contexts"})
            seen.add(name)
    observed = ["call-context"] + (["gpu-hotspot"] if any(h["kind"] == "gpu-kernel" for h in hotspots) else [])
    observed += [cap for prefix, cap in (("gxcopy", "transfer"), ("gsync", "sync"))
                 if any(m["metric"].startswith(prefix) for m in totals)]
    if stalls:
        observed.append("instruction-stalls")
        if any(s["source"].get("file") and s["source"].get("line") for s in stalls):
            observed.append("gpu-source-line")
        if any(any(token in s["metric"].lower() for token in ("lat_gmem", "lat_tmem", "long_scoreboard")) for s in stalls):
            observed.append("memory-latency")
    return {"hotspots": hotspots[:12], "contexts": sorted(records, key=lambda r: r["kernel_time_s"], reverse=True)[:12],
            "metric": chosen, "operation_totals": totals[:16],
            "stalls": stalls[:8],
            "context_count": len(contexts), "gpu_context_count": len(records),
            "observed_capabilities": observed,
            "limitations": ["GPU operation sums are not GPU busy union or ROI wall time.",
                            "No achieved-occupancy or cache-bandwidth inference from context metrics."]}


if __name__ == "__main__":
    import argparse
    from experiment_support import write
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", type=Path)
    source.add_argument("--advice", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kernel-regex")
    args = parser.parse_args()
    result = reduce_gpa(args.advice, args.kernel_regex) if args.advice else reduce_contexts(*read_v4(args.database))
    write(args.output, result)
