"""
Real DrGPUM `memory_liveness` analysis.

This package replaces the earlier `helpers/parse_drgpum_csv.py`, which parsed a
`kernel_name,runtime_ms,sm_utilization_%,memory_bandwidth_%` schema that DrGPUM
does not produce and cannot produce -- it is a memory profiler and records no
timing or utilisation counters. See `parser.py` for the formats DrGPUM actually
writes, all derived from the redshow source.

Usage:

    from agent.drgpum import parse_profile, analyze, format_report

    profile  = parse_profile("drgpum_out/")
    findings = analyze(profile)
    print(format_report(profile, findings))
"""

from .parser import (
    DrGPUMParseError,
    MemoryObject,
    MemoryOp,
    Profile,
    human_bytes,
    parse_profile,
)
from .detectors import ALL_DETECTORS, Finding, analyze
from .report import format_report, to_json

__all__ = [
    "ALL_DETECTORS",
    "DrGPUMParseError",
    "Finding",
    "MemoryObject",
    "MemoryOp",
    "Profile",
    "analyze",
    "format_report",
    "human_bytes",
    "parse_profile",
    "to_json",
]
