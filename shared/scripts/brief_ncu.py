#!/usr/bin/env python3
"""Render compact, decision-bearing Nsight Compute lines for the agent brief."""

from brief_facts import fmt_ms
from experiment_support import nested


def _fmt(value):
    return f"{value:.3g}" if isinstance(value, (int, float)) else "null"


def render_ncu(ncu, stall_limit=3, rule_limit=3, include_mem=True, include_diag=True):
    if not ncu:
        return []
    stalls_items = [item for item in (ncu.get("stalls") or []) if item.get("value") is not None]
    stalls = ",".join(f"{item['reason']}={item['value']:.3g}"
                      for item in stalls_items[:max(0, stall_limit)]) or "none"
    memory = ncu.get("memory") or {}
    advice = ncu.get("advice") or {}
    primary = (advice.get("primary") or {}).get("name")
    chain = next((item for item in (advice.get("models") or [])
                  if item.get("name") == "roofline-to-amdahl"), {})
    hierarchy = chain.get("hierarchy") or {}
    roof = hierarchy.get("roofline") or {}
    roof_text = (f"{roof.get('precision')}/{roof.get('classification')} "
                 f"K≤{_fmt((hierarchy.get('kernel') or {}).get('speedup_ceiling'))}x "
                 f"ROI≤{_fmt((hierarchy.get('application') or {}).get('speedup_ceiling'))}x")
    dram_r, dram_w = memory.get("dram_read_bytes"), memory.get("dram_write_bytes")
    r_gb = dram_r / 1e9 if isinstance(dram_r, (int, float)) else None
    w_gb = dram_w / 1e9 if isinstance(dram_w, (int, float)) else None
    mem = (f"NCU MEM: ldB/sector={_fmt(memory.get('global_load_bytes_per_sector'))}/32 "
           f"DRAM R/W={_fmt(r_gb)}/{_fmt(w_gb)}GB "
           f"L1/L2={_fmt(memory.get('l1_hit_pct'))}/{_fmt(memory.get('l2_hit_pct'))}% "
           f"spill={_fmt(memory.get('spill_requests_pct'))}%.")
    source = ncu.get("source_hotspot") or {}
    source_stall = (source.get("dominant_not_issued_stall") or {}).get("reason")
    source_signal = (f" lanes={_fmt(source.get('avg_threads_executed'))}"
                     f" div={_fmt(source.get('divergent_branches'))}"
                     f" srcstall={source_stall or 'null'}")
    alternatives = ",".join(item.get("name") for item in (advice.get("alternatives") or [])
                            if item.get("name")) or "none"
    diag = f"GPA: primary={primary}; alt={alternatives}; roof={roof_text}."
    lines = [
        f"NCU: sections={','.join(ncu.get('section') or [])}; cost={_fmt(ncu.get('total_s'))}s; metrics={ncu.get('metric_count')}.",
        (f"NCU: kernel={fmt_ms((ncu.get('duration_ns') or 0)/1e9)}ms "
         f"r/th={nested(ncu,'launch','registers_per_thread')} "
         f"occ={_fmt(nested(ncu,'occupancy','achieved_pct'))}% elig/issue="
         f"{_fmt(nested(ncu,'scheduler','eligible_warps'))}/{_fmt(nested(ncu,'scheduler','issue_active_pct'))}% "
         f"stall={stalls}{source_signal}."),
    ]
    if include_mem:
        lines.append(mem)
    if include_diag:
        lines.append(diag)
    return lines
