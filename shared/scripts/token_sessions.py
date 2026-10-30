"""Read measured session usage from Claude Code or Codex records."""

import json
import os
import pathlib

from token_counting import REPO

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")

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
