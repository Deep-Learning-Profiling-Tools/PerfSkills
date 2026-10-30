#!/usr/bin/env python3
"""Reduce a Codex JSONL rollout to measured token and interaction costs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

def text_blocks(value):
    if not isinstance(value, list):
        return []
    return [block.get("text", "") for block in value
            if isinstance(block, dict) and isinstance(block.get("text"), str)]

def preview_tool_input(raw: str, limit: int = 180) -> str:
    compact = " ".join(raw.split())
    compact = re.sub(r"/projects/[^\s\"']+", "<path>", compact)
    return compact[:limit]

def tool_patterns(raw: str) -> list[str]:
    patterns = [
        ("broad-reference-read", r"(?:cat|sed|rg).*(?:references/(?:registry|automation)\.md|shared/schema/)"),
        ("manual-plan-mutation", r"apply_patch.*(?:preflight|harness|experiment)-plan\.json"),
        ("framework-source-read", r"(?:cat|sed|rg).*shared/scripts/.*\.py"),
        ("source-slice-read", r"source-slices/|(?:baseline|candidates)/[^\s\"']+\.(?:c|cc|cpp|cu|cuh|f90)"),
        ("raw-artifact-read", r"(?:cat|sed|jq|rg).*runs/[^\s\"']+/raw/"),
        ("experiment-controller", r"shared/scripts/experiment\.py"),
        ("direct-finalizer", r"shared/scripts/finalize\.py"),
        ("ncu", r"pw-nsight-compute|\bncu\b"),
        ("inline-program", r"python(?:3)?\s+(?:-|-[a-zA-Z]*c)\b|<<\s*['\"]?[A-Za-z]+"),
    ]
    return [name for name, expression in patterns if re.search(expression, raw, re.I | re.S)]

def usage_segments(records: list[dict]) -> list[dict]:
    """Split cumulative token counters when a later user turn resets them."""
    segments, prior, current = [], None, None
    for record in records:
        usage = record.get("turn_token_usage") or {}
        total = usage.get("total_tokens")
        if not isinstance(total, int):
            continue
        if prior is not None and total < prior and current:
            segments.append(current)
        current = usage
        prior = total
    if current:
        segments.append(current)
    result = []
    for index, usage in enumerate(segments, 1):
        uncached_input = ((usage.get("input_tokens") or 0) -
                          (usage.get("cached_input_tokens") or 0))
        result.append({"turn": index, **usage, "uncached_input_tokens": uncached_input,
                       "uncached_total_tokens": uncached_input + (usage.get("output_tokens") or 0)})
    return result

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def audit(path: Path):
    counts = Counter()
    response_counts = Counter()
    calls = {}
    outputs = []
    usage_records = []
    message_chars = Counter()
    pattern_calls = Counter()
    pattern_inputs = Counter()
    pattern_outputs = Counter()
    parse_errors = 0

    with path.open(errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            kind = item.get("type", "unknown")
            counts[kind] += 1
            payload = item.get("payload") or {}
            if kind == "token_usage_record":
                usage_records.append({"line": line_number, **payload})
                continue
            if kind != "response_item":
                continue
            response_kind = payload.get("type", "unknown")
            response_counts[response_kind] += 1
            if response_kind == "custom_tool_call":
                raw_input = payload.get("input") or ""
                tags = tool_patterns(raw_input)
                calls[payload.get("call_id")] = {
                    "line": line_number,
                    "tool": payload.get("name"),
                    "input_chars": len(raw_input),
                    "input_preview": preview_tool_input(raw_input),
                    "patterns": tags,
                }
                for tag in tags:
                    pattern_calls[tag] += 1
                    pattern_inputs[tag] += len(raw_input)
            elif response_kind == "custom_tool_call_output":
                text = "".join(text_blocks(payload.get("output")))
                call = calls.get(payload.get("call_id"), {})
                outputs.append({
                    "line": line_number,
                    "call_id": payload.get("call_id"),
                    "tool": call.get("tool"),
                    "call_line": call.get("line"),
                    "chars": len(text),
                    "call_preview": call.get("input_preview"),
                })
                for tag in call.get("patterns", []):
                    pattern_outputs[tag] += len(text)
            elif response_kind == "message":
                text = "".join(text_blocks(payload.get("content")))
                message_chars[f"{payload.get('role')}:{payload.get('phase') or 'none'}"] += len(text)

    final_usage = usage_records[-1] if usage_records else {}
    cumulative = final_usage.get("turn_token_usage") or {}
    per_call = final_usage.get("usage") or {}
    outputs.sort(key=lambda item: item["chars"], reverse=True)
    largest_inputs = sorted(calls.values(), key=lambda item: item["input_chars"], reverse=True)
    total_output_chars = sum(item["chars"] for item in outputs)
    total_call_input_chars = sum(item["input_chars"] for item in calls.values())
    segments = usage_segments(usage_records)
    return {
        "protocol": "profileweaver.session-audit/v1",
        "source": {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "json_lines": sum(counts.values()) + parse_errors,
            "parse_errors": parse_errors,
        },
        "interaction": {
            "records": dict(counts),
            "response_items": dict(response_counts),
            "model_responses_with_usage": len(usage_records),
            "tool_calls": len(calls),
            "tool_input_chars": total_call_input_chars,
            "tool_output_chars": total_output_chars,
            "message_chars": dict(message_chars),
            "tool_patterns": {
                name: {"calls": pattern_calls[name], "input_chars": pattern_inputs[name],
                       "output_chars": pattern_outputs[name]}
                for name in sorted(pattern_calls)
            },
        },
        "tokens": {
            "cumulative_turn": cumulative,
            "last_response": per_call,
            "turns": segments,
            "largest_uncached_turn": (max(segments, key=lambda item: item["uncached_total_tokens"])
                                        if segments else None),
            "interpretation": {
                "input_tokens": "sum of input tokens read by all model responses in this turn",
                "cached_input_tokens": "input tokens served from prompt cache; still counted as input",
                "cache_write_input_tokens": "final cumulative newly written prompt-cache tokens",
                "end_to_end_comparable_only_with_same_runtime": True,
            },
        },
        "largest_tool_outputs": outputs[:20],
        "largest_tool_inputs": largest_inputs[:20],
    }

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()
    result = audit(args.session.resolve())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    cumulative = result["tokens"]["cumulative_turn"]
    summary = {
        "status": "ok",
        "output": str(args.out),
        "input_tokens": cumulative.get("input_tokens"),
        "new_context_tokens": cumulative.get("cache_write_input_tokens"),
        "tool_calls": result["interaction"]["tool_calls"],
        "tool_output_chars": result["interaction"]["tool_output_chars"],
    }
    print(json.dumps(summary if not args.print_summary else result,
                     separators=(",", ":") if not args.print_summary else None))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
