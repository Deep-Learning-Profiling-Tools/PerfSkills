"""Read resource use from the measured CUDA binary without changing build flags."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from experiment_support import expand, load, resolve, sha256, write
from acquisition_limits import remaining


def parse_resources(text: str) -> list[dict]:
    rows, symbol = [], None
    for line in text.splitlines():
        match = re.search(r"^\s*Function\s*:?\s+(\S+?)\s*:?\s*$", line)
        if match:
            symbol = match.group(1)
        values = dict(re.findall(r"\b(REG|STACK|SHARED|LOCAL):(\d+)", line))
        if symbol and "REG" in values:
            rows.append({"symbol": symbol, "registers": int(values["REG"]),
                         **{key: int(values[tag]) for tag, key in
                            (("STACK", "stack_bytes"), ("LOCAL", "local_bytes"),
                             ("SHARED", "shared_bytes")) if tag in values}})
            symbol = None
    return rows


def collect(plan: dict, run: Path, workspace: Path, candidate_id: str,
            parent_id: str = "baseline") -> dict:
    output = run / "compiler" / f"{candidate_id}.json"
    values = {"workspace": str(workspace), "run_dir": str(run),
              "run_id": plan["run_id"], "id": candidate_id}
    execution = plan["execution"]
    paths = [execution["command"][0], *(plan.get("kernel_profile") or {}).get("fingerprint_inputs", [])]
    binary = None
    for value in paths:
        expanded = Path(expand(value, values))
        path = (expanded if expanded.is_absolute() else
                resolve(execution.get("cwd", "{workspace}"), values) / expanded)
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    binary = path
                    break
    tool = shutil.which("cuobjdump")
    if not tool or binary is None:
        return {"status": "unavailable", "reason": "cuobjdump-or-cuda-binary-missing", "spent_s": 0}
    digest = sha256(binary)
    anchors = ({"baseline": sha256(run / "compiler/baseline.json"),
                "parent": sha256(run / "compiler" / f"{parent_id}.json")}
               if candidate_id != "baseline" else {})
    cached = load(output, {})
    if (cached.get("binary_sha256") == digest and cached.get("normalization_revision") == 2 and cached.get("status") == "complete"
            and cached.get("anchors") == anchors):
        return cached | {"spent_s": 0, "cache": "hit"}
    raw = run / "raw/compiler"
    raw.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        proc = subprocess.run([tool, "--dump-resource-usage", str(binary)],
                              capture_output=True, text=True, timeout=remaining(15))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__,
                "spent_s": time.perf_counter() - started}
    (raw / f"{candidate_id}.stdout.txt").write_text(proc.stdout)
    (raw / f"{candidate_id}.stderr.txt").write_text(proc.stderr)
    rows = parse_resources(proc.stdout) if proc.returncode == 0 else []
    pattern = ((plan.get("kernel_profile") or {}).get("variant_kernel_regex") or "")
    demangler = shutil.which("c++filt")
    if rows and demangler:
        try:
            demangled = subprocess.run([demangler], input="\n".join(x["symbol"] for x in rows),
                                       text=True, capture_output=True, timeout=remaining(5))
            for row, name in zip(rows, demangled.stdout.splitlines()):
                row["name"] = name
        except (OSError, subprocess.TimeoutExpired):
            pass
    unique = {tuple(sorted(x.items())): x for x in rows}
    rows = list(unique.values())
    matching = [x for x in rows if re.search(pattern, x.get("name", x["symbol"]))]
    selected = (matching or rows)[:8]
    baseline = load(run / "compiler/baseline.json", {})
    prior = {x["symbol"]: x for x in baseline.get("kernels", [])}
    delta = []
    for row in selected:
        before = prior.get(row["symbol"])
        if before:
            delta.append({"symbol": row["symbol"], **{
                k: row[k] - before[k] for k in
                ("registers", "local_bytes", "stack_bytes", "shared_bytes")
                if k in row and k in before}})
    result = {"status": "complete" if rows else "unavailable", "tool": "cuobjdump",
              "normalization_revision": 2,
              "binary_sha256": digest, "spent_s": time.perf_counter() - started,
              "anchors": anchors,
              "kernels": selected, "all_kernels": rows, "delta_baseline": delta,
              "scope": "static-resources-only", "runtime_stalls": None,
              "artifact": str(output)}
    parent = load(run / "compiler" / f"{parent_id}.json", {})
    parents = {x["symbol"]: x for x in parent.get("kernels", [])}
    result["delta_parent"] = [{"symbol": x["symbol"], **{
        k: x[k] - parents[x["symbol"]][k] for k in
        ("registers", "local_bytes", "stack_bytes", "shared_bytes")
        if k in x and k in parents[x["symbol"]]}}
        for x in selected if x["symbol"] in parents]
    write(output, result)
    return result


def for_target(resources: dict, name: str | None) -> dict:
    """Match an observed kernel, rather than leading with an arbitrary variant."""
    if not name:
        return resources | {"kernels": [], "delta_baseline": [], "delta_parent": [],
                            "active_target": None}
    from kernel_identity import matching
    rows, identity = matching(resources, name)
    symbols = {x["symbol"] for x in rows}
    return resources | {"kernels": rows, "active_target": name if rows else None, "identity_match": identity,
                         "delta_baseline": [x for x in resources.get("delta_baseline", []) if x["symbol"] in symbols],
                         "delta_parent": [x for x in resources.get("delta_parent", []) if x["symbol"] in symbols]}
