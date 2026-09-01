#!/usr/bin/env python3
"""Measure what ProfileWeaver's skills cost to load, and record a run's end-to-end consumption.

Two figures, kept strictly apart because conflating them fabricates a number:

  1. SKILL DEFINITION COST - what injecting a SKILL.md into context costs, counted once per
     load. Exact: it is a file on disk.
  2. END-TO-END SESSION COST - every token the investigation actually spent. Read from the
     agent's OWN records, not estimated:
       claude -> ~/.claude/projects/<slug>/<session>.jsonl, which carries real per-message
                 `usage` from the API, plus Claude Code's own `attributionSkill` field, so
                 tokens can be attributed to the skill that caused them.
       codex  -> ~/.codex/goals_1.sqlite, table thread_goals (tokens_used, token_budget).
                 UNVERIFIED: empty on this machine, never validated against a live Codex run.
     Whatever the CLI did not record stays null.

Cache reads dominate any long session and bill at a reduced rate, so `uncached_total`
(input+output) is reported as the headline and `all_inclusive_total` alongside it. Quoting the
all-inclusive figure as "tokens consumed" overstates cost by orders of magnitude.

Usage:
    tokens.py skills                       static cost of every skill in the tree
    tokens.py skills --loaded profile-weaver:3 pw-gpa:1
                                           ...weighted by how many times each was injected
    tokens.py session [--loaded ...]       + real end-to-end usage from the agent's records
    tokens.py run <run-id> [--loaded ...] [--session-total N --source "..."] [--wall-clock-s S]
                                           write runs/<run-id>/cost.json

Counting is per-agent, because the same file is a different number of tokens under each
tokenizer - quoting one agent's count for the other is a fabricated number:

    --agent claude   Anthropic count_tokens API   EXACT   needs `anthropic` + ANTHROPIC_API_KEY
    --agent codex    tiktoken o200k_base          EXACT   needs `tiktoken`; local, no API call
    (neither available)  len(text)/4              ROUGH   +-20%, labelled as an estimate

Default is --agent auto, which detects the *running* agent from its environment. The agent and
method are recorded in every output. Do not quote a heuristic figure as a measurement.
"""

import argparse
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SKILLS = REPO / ".claude" / "skills"
HEURISTIC_CHARS_PER_TOKEN = 4


CLAUDE_COUNT_MODEL = "claude-opus-4-20250514"
CODEX_ENCODING = "o200k_base"          # GPT-4o / GPT-5 family


def detect_agent():
    """Which agent is *running* - not merely installed.

    CLAUDECODE / CLAUDE_CODE_ENTRYPOINT are set by a live Claude Code session. Codex's
    CODEX_HOME is only a config directory and is present even when Codex is not running, so it
    is deliberately not treated as a runtime marker; CODEX_SANDBOX is.
    """
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude"
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):
        return "codex"
    return "unknown"


def _counter(agent="auto"):
    """Return (count_fn, method_name, is_exact).

    Each agent is counted with its own tokenizer, because the same file is a different number
    of tokens under each - quoting one agent's count for the other is a fabricated number.

      claude -> Anthropic count_tokens API (no public local tokenizer exists)
      codex  -> tiktoken o200k_base, exact and local for the GPT-4o/5 family
    """
    if agent == "auto":
        agent = detect_agent()

    if agent == "claude":
        try:
            import anthropic
            if os.environ.get("ANTHROPIC_API_KEY"):
                client = anthropic.Anthropic()

                def count(text):
                    return client.messages.count_tokens(
                        model=CLAUDE_COUNT_MODEL,
                        messages=[{"role": "user", "content": text}],
                    ).input_tokens
                return count, f"anthropic count_tokens ({CLAUDE_COUNT_MODEL})", True
        except Exception:
            pass
        return ((lambda t: len(t) // HEURISTIC_CHARS_PER_TOKEN),
                f"heuristic len/{HEURISTIC_CHARS_PER_TOKEN} - Claude counts need `pip install "
                f"anthropic` + ANTHROPIC_API_KEY (no local Claude tokenizer exists)", False)

    if agent == "codex":
        try:
            import tiktoken
            enc = tiktoken.get_encoding(CODEX_ENCODING)
            return (lambda t: len(enc.encode(t))), f"tiktoken {CODEX_ENCODING}", True
        except Exception:
            pass
        return ((lambda t: len(t) // HEURISTIC_CHARS_PER_TOKEN),
                f"heuristic len/{HEURISTIC_CHARS_PER_TOKEN} - Codex counts need "
                f"`pip install tiktoken` (exact and offline)", False)

    return ((lambda t: len(t) // HEURISTIC_CHARS_PER_TOKEN),
            f"heuristic len/{HEURISTIC_CHARS_PER_TOKEN} - agent not detected; pass "
            f"--agent claude|codex for an exact count", False)


USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")


def claude_transcript(session_id=None, project_dir=None):
    """Locate this session's Claude Code transcript.

    Claude Code writes one JSONL per session under ~/.claude/projects/<slugified-cwd>/, with
    real per-message `usage` from the API - so end-to-end consumption does not have to be
    supplied by hand.
    """
    base = pathlib.Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return None
    slug = str(project_dir or REPO).replace("/", "-")
    d = base / slug
    if not d.is_dir():
        return None
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid and (d / f"{sid}.jsonl").exists():
        return d / f"{sid}.jsonl"
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def read_claude_usage(path):
    """Aggregate real usage, plus Claude Code's own per-skill attribution."""
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    per_skill = {}
    records = 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        u = msg.get("usage")
        if not u:
            continue
        records += 1
        skill = d.get("attributionSkill")
        if skill:
            per_skill.setdefault(skill, dict.fromkeys(USAGE_FIELDS, 0))
        for k in USAGE_FIELDS:
            v = u.get(k) or 0
            totals[k] += v
            if skill:
                per_skill[skill][k] += v
    return {
        "source": f"claude code transcript {path}",
        "usage_records": records,
        **totals,
        # Uncached work is the honest headline; cache reads are billed at a reduced rate and
        # dwarf everything else, so summing them into one "total" misleads.
        "uncached_total": totals["input_tokens"] + totals["output_tokens"],
        "all_inclusive_total": sum(totals.values()),
        "per_skill": per_skill,
    }


def read_codex_usage():
    """Codex records per-goal token accounting in ~/.codex/goals_1.sqlite (thread_goals)."""
    db = pathlib.Path.home() / ".codex" / "goals_1.sqlite"
    if not db.exists():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = list(con.execute(
            "select thread_id, goal_id, tokens_used, token_budget, time_used_seconds "
            "from thread_goals"))
        con.close()
    except Exception as e:
        return {"source": f"codex {db}", "error": str(e)}
    if not rows:
        return {"source": f"codex {db}", "usage_records": 0,
                "note": "thread_goals is empty - no Codex run recorded here. Schema supports "
                        "tokens_used but this path is UNVERIFIED against a real Codex session."}
    return {
        "source": f"codex {db}",
        "usage_records": len(rows),
        "tokens_used": sum(r[2] or 0 for r in rows),
        "goals": [{"thread_id": r[0], "goal_id": r[1], "tokens_used": r[2],
                   "token_budget": r[3], "time_used_seconds": r[4]} for r in rows],
        "note": "UNVERIFIED path - validate against a real Codex session before quoting.",
    }


def read_session_usage(agent, session_id=None, project_dir=None):
    if agent == "claude":
        p = claude_transcript(session_id, project_dir)
        if not p:
            return {"source": None, "note": "no Claude Code transcript found for this project"}
        return read_claude_usage(p)
    if agent == "codex":
        return read_codex_usage() or {"source": None, "note": "no Codex goals db found"}
    return {"source": None, "note": f"agent '{agent}' not detected; cannot read a transcript"}


def measure_skills(count, parse_loaded=None):
    """Per-skill definition cost: SKILL.md plus any references/ and assets/ it may pull in."""
    out = []
    for d in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        skill_md = d / "SKILL.md"
        if not skill_md.exists():
            continue
        text = skill_md.read_text()
        entry = {
            "skill": d.name,
            "skill_md_chars": len(text),
            "skill_md_tokens": count(text),
            "on_demand": [],
        }
        for sub in ("references", "assets"):
            for f in sorted((d / sub).glob("*")) if (d / sub).is_dir() else []:
                if f.is_file():
                    t = f.read_text(errors="replace")
                    entry["on_demand"].append({
                        "file": f"{sub}/{f.name}",
                        "chars": len(t),
                        "tokens": count(t),
                    })
        entry["on_demand_tokens_total"] = sum(x["tokens"] for x in entry["on_demand"])
        if parse_loaded is not None:
            n = parse_loaded.get(d.name, 0)
            entry["loads_this_session"] = n
            entry["tokens_charged"] = entry["skill_md_tokens"] * n
        out.append(entry)
    return out


def parse_loaded_arg(items):
    loaded = {}
    for it in items or []:
        name, _, n = it.partition(":")
        loaded[name] = int(n) if n else 1
    return loaded


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
    print(f"\n  === end-to-end session consumption ===")
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
