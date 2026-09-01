"""Rendering for parsed profiles and findings."""

from __future__ import annotations

import json
from typing import Dict, List

from .detectors import Finding
from .parser import Profile, human_bytes


def profile_summary(profile: Profile) -> Dict:
    return {
        "source_dir": str(profile.source_dir) if profile.source_dir else None,
        "objects": len(profile.objects),
        "kernels": len(profile.kernel_ops),
        "total_allocated_bytes": profile.total_allocated_bytes(),
        "current_memory_peak": profile.current_memory_peak,
        "optimal_memory_peak": profile.optimal_memory_peak,
        "peak_gap_bytes": profile.peak_gap_bytes,
        "peak_waste_fraction": profile.peak_waste_fraction,
        "memory_peak_kernel": profile.memory_peak_kernel,
    }


def to_json(profile: Profile, findings: List[Finding], indent: int = 2) -> str:
    return json.dumps(
        {
            "profile": profile_summary(profile),
            "findings": [f.to_dict() for f in findings],
        },
        indent=indent,
    )


def format_report(profile: Profile, findings: List[Finding], top: int = 10) -> str:
    out = []
    add = out.append

    add("=" * 74)
    add("DrGPUM MEMORY-LIVENESS REPORT")
    add("=" * 74)
    add("")
    add(f"  Source            : {profile.source_dir}")
    add(f"  Memory objects    : {len(profile.objects)}")
    add(f"  Kernel launches   : {len(profile.kernel_ops)}")

    if profile.current_memory_peak is not None:
        add(f"  Peak memory       : {human_bytes(profile.current_memory_peak)}")
    if profile.optimal_peak_available:
        add(f"  Optimal peak      : {human_bytes(profile.optimal_memory_peak)}")
        gap = profile.peak_gap_bytes
        if gap is not None:
            frac = profile.peak_waste_fraction or 0.0
            add(f"  Recoverable       : {human_bytes(gap)}  ({frac:.1%} of peak)")
    else:
        add("  Optimal peak      : not computed by this run")
        add("                      (rank by per-object severity instead)")
    if profile.memory_peak_kernel is not None:
        add(f"  Peak reached at   : kernel {profile.memory_peak_kernel}")

    if not profile.access_tracking_available:
        add("")
        add("  ! No ACCESS operations recorded. Usage-based findings")
        add("    (NEVER_USED) are suppressed: without them every object")
        add("    looks unused. Re-profile with")
        add("    -ck HPCRUN_SANITIZER_LIVENESS_ONGPU=1")

    if profile.has_submemory:
        add(f"  Sub-allocations   : {len(profile.submemory)} (PyTorch tensors)")
        if profile.current_submemory_peak:
            add(f"  Tensor peak       : {human_bytes(profile.current_submemory_peak)}")
        add(f"  Python-attributed : {len(profile.python_states)} allocations")
    else:
        add("")
        add("  ! No sub-allocation data. For a PyTorch workload the objects")
        add("    above are caching-allocator segments, not tensors -- build")
        add("    torch-monitor and profile with")
        add("    -ck HPCRUN_SANITIZER_TORCH_ANALYSIS=1")

    if profile.peak_underflowed:
        add("")
        add("  ! This run's peak underflowed DrGPUM's own u64 arithmetic")
        add("    (output_ctx_node subtracts 512 from a peak below 512 B).")
        add("    Peak figures are unwrapped above but the run is too small")
        add("    to draw conclusions from. Profile a real workload.")

    add("")
    if not findings:
        add("  No inefficiencies above threshold. Lower --min-bytes or")
        add("  --min-idle-kernels to widen the search.")
        add("")
        return "\n".join(out)

    counts: Dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1
    add("  Findings by kind:")
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        add(f"    {kind:<22s} {n}")
    add("")

    add("-" * 74)
    add(f"TOP {min(top, len(findings))} FINDINGS (ranked by severity)")
    add("-" * 74)

    for i, f in enumerate(findings[:top], 1):
        add("")
        target = "whole run" if f.object_id is None else f"object {f.object_id}"
        add(f"{i}. {f.kind}  --  {target}")
        add(f"   size {human_bytes(f.size_bytes)} | idle {f.idle_kernels} kernels "
            f"| severity {f.severity:.1%}")
        add("")
        site = (
            profile.python_site(profile.objects[f.object_id].alloc_op)
            if f.object_id in profile.objects
            and profile.objects[f.object_id].alloc_op is not None
            else None
        )
        if site is not None:
            add(f"   SITE : {site}")
        add(f"   WHAT : {_wrap(f.evidence)}")
        add(f"   HARM : {_wrap(f.harm)}")
        add(f"   FIX  : {_wrap(f.fix)}")

    add("")
    add("-" * 74)
    add("Severity = idle byte-kernels as a share of (peak bytes x kernel launches).")
    add("Only objects live at the peak kernel can reduce peak memory.")
    add("-" * 74)
    return "\n".join(out)


def _wrap(text: str, width: int = 62, indent: str = " " * 10) -> str:
    import textwrap

    lines = textwrap.wrap(text, width=width)
    if not lines:
        return ""
    return ("\n" + indent).join(lines)
