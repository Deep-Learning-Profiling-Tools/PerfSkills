#!/usr/bin/env python3
"""The brief CLI; evidence extraction and rendering stay in private modules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from brief_builder import DEFAULT_MAX_TOKENS, build


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--phase", choices=["auto", "optimize", "iterate", "repair", "final"], default="auto")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--text-output", type=Path)
    parser.add_argument("--print-brief", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    machine, text = build(run, args.phase, args.max_tokens)
    json_output = args.json_output or run / "agent-brief.json"
    text_output = args.text_output or run / "agent-brief.txt"
    json_output.write_text(json.dumps(machine, indent=2) + "\n")
    text_output.write_text(text)
    if args.print_json:
        print(json.dumps(machine, indent=2))
    elif args.print_brief:
        print(text, end="")
    else:
        print(json.dumps({"status": "ok", "brief": str(text_output),
                          "tokens": machine["context"]["text_tokens"],
                          "action": machine["action"]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
