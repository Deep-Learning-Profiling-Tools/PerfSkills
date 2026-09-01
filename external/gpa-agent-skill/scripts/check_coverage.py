#!/usr/bin/env python3
"""Check whether a candidate CUDA benchmark name is already covered by
GvProf, DrGPUM, or GPA, against the cited snapshot in assets/known-coverage.json.

This deliberately never asserts a hard "uncovered" verdict -- GvProf/DrGPUM/GPA
don't publish one canonical "here is everything we've ever tested" list, so
absence from this snapshot is "not-found" (best available evidence), not proof
of non-coverage. See assets/known-coverage.json's _meta for how/when this
snapshot was gathered; refresh it if stale before trusting a "not-found" result
for something load-bearing.

Usage: check_coverage.py <candidate-name>
"""

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
COVERAGE_FILE = SKILL_DIR / "assets" / "known-coverage.json"


def normalize(name: str) -> str:
    return name.strip().lower().replace("_", "").replace("-", "").replace("+", "")


def check_tool(candidate: str, tool_key: str, tool_data: dict, resolved: dict) -> dict:
    known = resolved[tool_key]
    if not isinstance(known, list):
        return {"status": "unknown", "reason": f"'{tool_key}' has no independent benchmark list to check"}
    normalized_known = {normalize(b.split(" ")[0]): b for b in known}
    norm_candidate = normalize(candidate)
    for norm_known, original in normalized_known.items():
        if norm_candidate == norm_known or norm_candidate in norm_known or norm_known in norm_candidate:
            return {
                "status": "found",
                "matchedAs": original,
                "source": tool_data.get("benchmarkSuiteRepo") or tool_data.get("repo"),
            }
    return {
        "status": "not-found",
        "reason": "absent from this tool's known benchmark snapshot -- best available evidence, not proof",
        "source": tool_data.get("benchmarkSuiteRepo") or tool_data.get("repo"),
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: check_coverage.py <candidate-name>")
    candidate = sys.argv[1]

    data = json.loads(COVERAGE_FILE.read_text())
    meta = data["_meta"]

    # drgpum's knownBenchmarks is a string pointer ("identical to gvprof...") in the
    # source file to avoid duplicating the list -- resolve it before checking.
    resolved = {}
    for tool_key in ("gvprof", "drgpum", "gpa"):
        kb = data[tool_key]["knownBenchmarks"]
        resolved[tool_key] = data["gvprof"]["knownBenchmarks"] if isinstance(kb, str) else kb

    result = {
        "candidate": candidate,
        "snapshotLastChecked": meta["lastChecked"],
        "coverageCheck": {},
        "evidence": [],
    }
    for tool_key in ("gvprof", "drgpum", "gpa"):
        tool_result = check_tool(candidate, tool_key, data[tool_key], resolved)
        result["coverageCheck"][tool_key] = tool_result["status"]
        if tool_result.get("source"):
            result["evidence"].append(tool_result["source"])
        if tool_result["status"] == "found":
            result["coverageCheck"][f"{tool_key}MatchedAs"] = tool_result["matchedAs"]
    result["evidence"] = sorted(set(result["evidence"]))

    all_not_found = all(v == "not-found" for k, v in result["coverageCheck"].items() if k in ("gvprof", "drgpum", "gpa"))
    result["verdict"] = (
        "no-evidence-of-coverage" if all_not_found else "covered-by-at-least-one"
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
