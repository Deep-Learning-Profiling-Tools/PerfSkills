"""Agent-aware skill definition token accounting."""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / ".claude" / "skills"
HEURISTIC_CHARS_PER_TOKEN = 4
CLAUDE_COUNT_MODEL = "claude-opus-4-20250514"
CODEX_ENCODING = "o200k_base"

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
