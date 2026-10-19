#!/usr/bin/env python3
"""Create a reproducible baseline fingerprint from files and contract values."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def digest_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path.resolve()), "kind": "file", "bytes": path.stat().st_size,
            "sha256": digest.hexdigest()}


def digest_directory(path: Path):
    digest = hashlib.sha256()
    count = total = 0
    for item in sorted(x for x in path.rglob("*") if x.is_file() and "__pycache__" not in x.parts):
        relative = str(item.relative_to(path)).encode()
        file_digest = digest_file(item)
        digest.update(len(relative).to_bytes(8, "big")); digest.update(relative)
        digest.update(bytes.fromhex(file_digest["sha256"]))
        count += 1; total += file_digest["bytes"]
    return {"path": str(path.resolve()), "kind": "directory", "files": count,
            "bytes": total, "sha256": digest.hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", action="append", type=Path, default=[])
    parser.add_argument("--directory", action="append", type=Path, default=[])
    parser.add_argument("--value", action="append", default=[], help="literal NAME=VALUE contract item")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    missing = [str(path) for path in [*args.file, *args.directory] if not path.exists()]
    if missing:
        raise SystemExit("missing fingerprint inputs: " + ", ".join(missing))
    artifacts = [digest_file(path) for path in args.file] + [digest_directory(path) for path in args.directory]
    values = {}
    for value in args.value:
        key, separator, item = value.partition("=")
        if not separator or not key:
            raise SystemExit(f"--value must be NAME=VALUE, got {value!r}")
        values[key] = item
    canonical = json.dumps({"artifacts": artifacts, "values": values}, sort_keys=True, separators=(",", ":")).encode()
    result = {"protocol": "profileweaver.baseline-fingerprint/v1",
              "generated_utc": datetime.now(timezone.utc).isoformat(),
              "artifacts": artifacts, "values": values,
              "contract_sha256": hashlib.sha256(canonical).hexdigest()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "ok", "output": str(args.out),
                      "artifacts": len(artifacts), "contract_sha256": result["contract_sha256"]},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
