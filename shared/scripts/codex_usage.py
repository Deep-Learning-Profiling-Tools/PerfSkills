"""Read only a specifically identified session's recorded cumulative token counters."""

import json
import os
from pathlib import Path


def locate(session):
    session = session or os.environ.get("CODEX_THREAD_ID")
    if not session:
        return None
    direct = Path(session)
    if direct.is_file():
        return direct
    root = Path(os.environ.get("CODEX_HOME") or Path.home()/".codex")
    matches = list((root/"sessions").glob("*/*/*/*"+session+".jsonl"))
    return matches[0] if len(matches) == 1 else None


def read(session=None):
    path = locate(session)
    if path is None:
        return {"source": None, "total_tokens": None, "note": "session-specific client usage unavailable"}
    last, timestamp = None, None
    with path.open(errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if not isinstance(row,dict):
                    continue
                payload = row.get("payload") or {}
                if isinstance(payload,dict) and row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    usage = (payload.get("info") or {}).get("total_token_usage")
                    if usage:
                        last, timestamp = usage, row.get("timestamp")
            except (ValueError, TypeError):
                continue
    if not last:
        return {"source": str(path), "total_tokens": None, "note": "no recorded cumulative token counters"}
    required = ["input_tokens", "cached_input_tokens", "output_tokens"]
    if not all(type(last.get(k)) is int and last[k] >= 0 for k in required):
        return {"source": str(path), "total_tokens": None, "note": "incomplete cumulative token counters"}
    uncached = last["input_tokens"]-last["cached_input_tokens"]
    if uncached < 0:
        return {"source": str(path), "total_tokens": None, "note": "inconsistent cached/input counters"}
    return {"source": str(path), **last, "uncached_total": uncached+last["output_tokens"],
            "all_inclusive_total": last["input_tokens"]+last["output_tokens"],
            "as_of": timestamp, "scope": "session-counter-as-of-timestamp",
            "convention": "uncached input plus output; output includes reasoning"}
