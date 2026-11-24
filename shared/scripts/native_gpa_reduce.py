"""Reduce literal GPA instruction advice without inventing timing."""

import importlib.util
import re
import shutil
import subprocess
import math
from pathlib import Path
from experiment_support import REPO


def reduce_gpa(advice: Path, target_regex=None) -> dict:
    parser_path = REPO / "external/gpa-agent-skill/scripts/parse_advice.py"
    spec = importlib.util.spec_from_file_location("pw_gpa_advice_parser", parser_path)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    blocks = parser.dedupe_blocks(parser.parse_kernel_blocks(advice.read_text(errors="replace")))
    demangler = shutil.which("c++filt")
    if blocks and demangler:
        names = subprocess.run([demangler], input="\n".join(b["signature"] for b in blocks),
                               text=True, capture_output=True, timeout=5)
        if names.returncode == 0:
            for block, name in zip(blocks, names.stdout.splitlines()):
                block["signature"] = name
    if target_regex:
        blocks = [b for b in blocks if re.search(target_regex, b["signature"])]
    if not blocks:
        raise ValueError("GPA advice has no recognized kernel records")
    findings, informational = parser.build_findings(blocks)
    selected = []
    for item in findings + informational:
        locations = []
        for loc in item.get("locations", []):
            literal = {key: loc.get(key) for key in ("file", "line", "ratio", "stallReason")}
            if literal not in locations:
                locations.append(literal)
        selected.append({"kernel": item["kernel"], "optimizer": item.get("optimizer"),
                         "reported_ratio_pct": item.get("ratio"),
                         "stall_token": item.get("dominantStallReason"),
                         "locations": locations[:4],
                         "advisor_predicted_speedup": item.get("speedup"),
                         "prediction_role": "tool-prediction-not-measured-speedup"})
    hotspots = [{"rank": i, "name": b["signature"], "kind": "gpu-kernel", "self_time_s": None,
                 "pct_of_wall": None, "calls": None} for i, b in enumerate(blocks[:12], 1)]
    for hotspot in hotspots:
        location = next((loc for finding in selected if finding["kernel"] == hotspot["name"]
                         for loc in finding["locations"] if loc.get("file") and loc.get("line")), None)
        if location:
            hotspot.update({"file": location["file"], "line": location["line"]})
    observed = []
    if any(f.get("stall_token") for f in selected):
        observed.append("instruction-stalls")
    if any(loc.get("file") and loc.get("line") for f in selected for loc in f["locations"]):
        observed.append("gpu-source-line")
    if all(cap in observed for cap in ("instruction-stalls", "gpu-source-line")):
        observed.append("causal-stall-attribution")
    if any(str(f.get("stall_token", "")).startswith(("GINS:LAT_GMEM", "GINS:LAT_TMEM")) for f in selected):
        observed.append("memory-latency")
    return {"hotspots": hotspots, "findings": selected[:5], "kernel_count": len(blocks),
            "observed_capabilities": observed,
            "limitations": ["Advisor predictions are not measured speedups; timings remain unknown.",
                            "Sampled stall attribution is not a host timeline or hardware roofline."]}

