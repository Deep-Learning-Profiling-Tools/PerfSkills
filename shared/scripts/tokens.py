#!/usr/bin/env python3
"""Report skill-definition cost and measured end-to-end session tokens."""

import argparse
import json
import sys

from token_counting import REPO, _counter, detect_agent, measure_skills, parse_loaded_arg
from token_sessions import USAGE_FIELDS, read_session_usage

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["skills", "session", "run"])
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--loaded", nargs="*", metavar="SKILL[:N]",
                    help="skills injected this session, with load counts (e.g. profile-weaver:3)")
    ap.add_argument("--session-total", type=int, default=None,
                    help="authoritative total tokens for the whole investigation (from /cost)")
    ap.add_argument("--session-input", type=int, default=None)
    ap.add_argument("--session-output", type=int, default=None)
    ap.add_argument("--wall-clock-s", type=float, default=None)
    ap.add_argument("--source", default=None,
                    help="where the session numbers came from, e.g. 'claude code /cost'")
    ap.add_argument("--agent", choices=["auto", "claude", "codex"], default="auto",
                    help="which tokenizer to count with: claude -> Anthropic count_tokens API, "
                         "codex -> tiktoken o200k_base. Default auto-detects the running agent.")
    ap.add_argument("--session-id", default=None, help="transcript to read (default: newest)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    a = ap.parse_args()

    agent = detect_agent() if a.agent == 'auto' else a.agent
    count, method, exact = _counter(a.agent)
    loaded = parse_loaded_arg(a.loaded) if a.loaded else None
    skills = measure_skills(count, loaded)

    # Real end-to-end consumption, read from the agent's own transcript. Anything the CLI did
    # not record stays null - never substitute the skill-definition figure for it.
    session = read_session_usage(agent, a.session_id)
    if a.session_total is not None:
        session["total_tokens_supplied"] = a.session_total
        session["supplied_source"] = a.source
    if a.session_input is not None:
        session["input_tokens_supplied"] = a.session_input
    if a.session_output is not None:
        session["output_tokens_supplied"] = a.session_output
    if a.wall_clock_s is not None:
        session["wall_clock_s"] = a.wall_clock_s

    tree_total = sum(s["skill_md_tokens"] for s in skills)
    charged = sum(s.get("tokens_charged", 0) for s in skills) if loaded else None

    payload = {
        "agent": agent,
        "method": method,
        "exact": exact,
        "tree_skill_md_tokens": tree_total,
        "skills": skills,
        "skill_definition_tokens_charged": charged,
        "session": session,
    }
    denom = session.get("uncached_total") or a.session_total
    if charged is not None and denom:
        payload["skill_definition_share_of_uncached"] = round(charged / denom, 4)

    if a.mode == "run":
        if not a.run_id:
            ap.error("run mode needs a run id")
        d = REPO / "runs" / a.run_id
        if not d.is_dir():
            ap.error(f"no run directory at {d}")
        (d / "cost.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {d / 'cost.json'}")

    if a.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"\n  agent:  {agent}")
    print(f"  method: {method}{'' if exact else '   <- NOT exact; label it as an estimate'}\n")
    hdr = f"  {'skill':<18} {'SKILL.md':>9} {'on-demand':>10}"
    if loaded:
        hdr += f" {'loads':>6} {'charged':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for s in skills:
        line = f"  {s['skill']:<18} {s['skill_md_tokens']:>9,} {s['on_demand_tokens_total']:>10,}"
        if loaded:
            line += f" {s.get('loads_this_session', 0):>6} {s.get('tokens_charged', 0):>9,}"
        print(line)
    print("  " + "-" * (len(hdr) - 2))
    print(f"  {'whole tree':<18} {tree_total:>9,}")
    if charged is not None:
        print(f"  {'charged':<18} {'':>9} {'':>10} {'':>6} {charged:>9,}")
        unloaded = tree_total - sum(s["skill_md_tokens"] for s in skills
                                    if s.get("loads_this_session", 0))
        print(f"\n  progressive disclosure saved ~{unloaded:,} tokens "
              f"({len([s for s in skills if not s.get('loads_this_session')])} skills never loaded)")
    print("\n  === end-to-end session consumption ===")
    if session.get("source"):
        print(f"  source: {session['source']}")
        if session.get("usage_records"):
            print(f"  {session['usage_records']} API usage records")
        for k in USAGE_FIELDS:
            if k in session:
                print(f"    {k:<30} {session[k]:>14,}")
        if "uncached_total" in session:
            print(f"    {'uncached (input+output)':<30} {session['uncached_total']:>14,}   <- the honest headline")
            print(f"    {'incl. cache reads':<30} {session['all_inclusive_total']:>14,}   <- cache reads bill at a reduced rate")
        if session.get("tokens_used") is not None:
            print(f"    {'tokens_used':<30} {session['tokens_used']:>14,}")
        for sk, u in (session.get("per_skill") or {}).items():
            print(f"\n  attributed by the CLI to skill '{sk}':")
            for k in USAGE_FIELDS:
                print(f"    {k:<30} {u[k]:>14,}")
    else:
        print(f"  not available: {session.get('note')}")
    if session.get("note") and session.get("source"):
        print(f"  note: {session['note']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
