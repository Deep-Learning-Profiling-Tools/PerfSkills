#!/usr/bin/env python3
"""Capture one bounded Nsight Compute kernel and export its raw metrics quietly."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(argv: list[str], *, cwd: Path, stdout: Path, stderr: Path,
        timeout: float | None) -> tuple[int, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = 124
        out = exc.stdout or b""
        err = (exc.stderr or b"") + b"\nProfileWeaver: command timed out\n"
    elapsed = time.perf_counter() - started
    stdout.write_bytes(out)
    stderr.write_bytes(err)
    return code, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kernel-name", required=True,
                        help="Exact or regex body for one kernel; regex mode is the default")
    parser.add_argument("--exact-kernel", action="store_true")
    parser.add_argument("--nvtx-include", default=None,
                        help="Verified NCU NVTX include expression used with --nvtx")
    parser.add_argument("--launch-skip", type=int, default=0)
    parser.add_argument("--launch-count", type=int, default=1)
    parser.add_argument("--section", action="append", default=["all"],
                        help="Section identifier; ProfileWeaver aliases 'all' to NCU set 'full'")
    parser.add_argument("--ncu", default="ncu")
    parser.add_argument("--tool-version", default=None,
                        help="Version captured by cached preflight; avoids another probe")
    parser.add_argument("--timeout-s", type=float, default=None)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("target command is required after --")
    if args.launch_count < 1:
        parser.error("--launch-count must be positive")
    if args.launch_skip < 0:
        parser.error("--launch-skip cannot be negative")

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_base = output / "capture"
    report = output / "capture.ncu-rep"
    sections = list(dict.fromkeys(args.section))
    if len(sections) > 1 and "all" in sections:
        sections.remove("all")
    selector: list[str]
    resolved_mode: dict
    if sections == ["all"]:
        # NCU has no section named "all". Its documented all-sections collection is set=full.
        selector = ["--set", "full"]
        resolved_mode = {"set": "full", "sections": None}
    else:
        selector = [value for section in sections for value in ("--section", section)]
        resolved_mode = {"set": None, "sections": sections}

    kernel_filter = args.kernel_name if args.exact_kernel else f"regex:{args.kernel_name}"
    capture_argv = [
        args.ncu, "--target-processes", "all", "--replay-mode", "kernel",
        "--kernel-name-base", "demangled", "--kernel-name", kernel_filter,
        "--launch-skip", str(args.launch_skip), "--launch-count", str(args.launch_count),
        *(["--nvtx", "--nvtx-include", args.nvtx_include] if args.nvtx_include else []),
        *selector, "--force-overwrite", "--export", str(report_base),
        "--log-file", str(output / "capture.log.txt"), *command,
    ]
    capture_code, capture_s = run(
        capture_argv, cwd=args.cwd.resolve(), stdout=output / "application.stdout.txt",
        stderr=output / "application.stderr.txt", timeout=args.timeout_s)

    exports: list[dict] = []
    export_total_s = 0.0
    if capture_code == 0 and report.is_file():
        for page, filename, extra in (
            ("raw", "metrics.csv", ["--print-units", "base"]),
            ("details", "details.csv", ["--print-details", "all", "--print-metric-name", "label-name"]),
            ("source", "source.csv", ["--print-source", "sass"]),
        ):
            path = output / filename
            argv = [args.ncu, "--import", str(report), "--csv", "--page", page,
                    *extra, "--log-file", str(path)]
            code, elapsed = run(argv, cwd=args.cwd.resolve(),
                                stdout=output / f"export-{page}.stdout.txt",
                                stderr=output / f"export-{page}.stderr.txt",
                                timeout=args.timeout_s)
            export_total_s += elapsed
            exports.append({"page": page, "path": str(path), "returncode": code,
                            "elapsed_s": round(elapsed, 6), "command": shlex.join(argv)})

    status = "complete" if (capture_code == 0 and report.is_file() and
                              any(item["page"] == "raw" and item["returncode"] == 0
                                  for item in exports)) else "failed"
    metadata = {
        "protocol": "profileweaver.ncu-capture/v1",
        "status": status,
        "tool": {"name": "NVIDIA Nsight Compute", "version": args.tool_version,
                 "executable": args.ncu},
        "requested": {"section": sections, "kernel_name": args.kernel_name,
                      "exact_kernel": args.exact_kernel, "launch_skip": args.launch_skip,
                      "launch_count": args.launch_count, "nvtx_include": args.nvtx_include},
        "resolved": resolved_mode | {"kernel_filter": kernel_filter, "replay_mode": "kernel"},
        "timing": {"capture_elapsed_s": round(capture_s, 6),
                   "export_elapsed_s": round(export_total_s, 6),
                   "total_elapsed_s": round(capture_s + export_total_s, 6)},
        "capture": {"returncode": capture_code, "command": shlex.join(capture_argv),
                    "report": str(report)},
        "exports": exports,
        "raw_dir": str(output),
    }
    metadata_path = output / "capture.json"
    write_json(metadata_path, metadata)
    # One bounded terminal record; application and profiler chatter stay under raw/.
    print(json.dumps({"status": status, "capture_s": round(capture_s, 3),
                      "export_s": round(export_total_s, 3), "metadata": str(metadata_path)},
                     separators=(",", ":")))
    return 0 if status == "complete" else 2


if __name__ == "__main__":
    sys.exit(main())
