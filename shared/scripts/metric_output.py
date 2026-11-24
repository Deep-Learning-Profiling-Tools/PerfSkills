"""Reveal the existing timer's value without altering application source/math."""

import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess

from experiment_support import REPO, load, sha256, write


def literal_prefix(pattern: str) -> str | None:
    result = ""; cursor = 1 if pattern.startswith("^") else 0
    while cursor < len(pattern):
        char = pattern[cursor]
        if char == "\\":
            cursor += 1
            if cursor >= len(pattern) or pattern[cursor].isalnum():
                break
            result += pattern[cursor]
        elif char in ".[](){}?*+|$":
            break
        else:
            result += char
        cursor += 1
    result = result.strip()
    return result if len(result) >= 4 and any(c.isalpha() for c in result) else None


def configure(plan: dict, run: Path) -> dict:
    if (plan.get("sampling") or {}).get("mode") != "adaptive":
        return plan
    metric = plan.get("metric") or {}
    prefix = literal_prefix(metric.get("regex", ""))
    status = {"status": "unavailable", "reason": "metric-not-supported",
              "role": "representation-precision-only", "timer_accuracy": None}
    configured = metric.get("output_precision", "auto")
    preload = (plan.get("execution", {}).get("env") or {}).get("LD_PRELOAD") or (plan.get("env") or {}).get("LD_PRELOAD") or os.environ.get("LD_PRELOAD")
    cc = shutil.which("cc")
    if configured is False:
        status["reason"] = "disabled"
    elif preload:
        status["reason"] = "existing-preload-preserved"
    elif metric.get("source") == "stdout" and prefix and platform.system() == "Linux" and cc:
        source = REPO / "shared/native/metric_output.c"
        identity = sha256(source) + str(Path(cc).resolve()) + str(Path(cc).stat().st_mtime_ns) + platform.machine() + str(os.confstr("CS_GNU_LIBC_VERSION"))
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        cache = REPO / "runs/_cache/metric-output" / key; cache.mkdir(parents=True, exist_ok=True)
        library = cache / "metric-output.so"
        if not library.exists():
            temporary = library.with_name(f"metric-output.{os.getpid()}.so")
            argv = [cc, "-std=c11", "-O2", "-fPIC", "-shared", str(source), "-o", str(temporary)]
            proc = subprocess.run(argv, capture_output=True, timeout=30)
            (cache / "build.stdout.txt").write_bytes(proc.stdout); (cache / "build.stderr.txt").write_bytes(proc.stderr)
            write(cache / "build.json", {"command": argv, "returncode": proc.returncode})
            if proc.returncode == 0:
                temporary.replace(library)
        if library.exists():
            status.update({"status": "configured", "reason": None, "prefix": prefix, "digits": 9,
                           "library": str(library), "sha256": sha256(library), "supported_calls": ["printf", "__printf_chk"]})
            plan["env"] = dict(plan.get("env") or {}) | {"LD_PRELOAD": str(library),
                "PW_METRIC_OUTPUT_PREFIX": prefix, "PW_METRIC_OUTPUT_DIGITS": "9"}
            kernel = plan.get("kernel_profile") or {}
            if kernel:
                kernel["fingerprint_inputs"] = list(kernel.get("fingerprint_inputs") or []) + [str(library)]
        else:
            status["reason"] = "formatter-build-failed"
    write(run / "measurement-output.json", status)
    return plan
