"""Inspect an existing CMake link binding and its named symbol exports."""

import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from experiment_support import REPO, load, resolve

_LIBRARY = re.compile(r"\.(?:a|so(?:\.[0-9.]+)?)$")
_SYMBOL = re.compile(r"[A-Za-z_$][\w.$@]*")


def _target(run, plan, cid, workspace):
    variables = {"repo": str(REPO), "run_id": plan["run_id"], "run_dir": str(run),
                 "workspace": str(workspace), "id": cid}
    candidates = (plan.get("kernel_profile") or {}).get("fingerprint_inputs") or []
    for spec in [*candidates, *plan["execution"]["command"]]:
        path = resolve(spec, variables).resolve()
        build = next((parent for parent in path.parents[:3] if (parent / "CMakeCache.txt").is_file()), None)
        if build and (build / "CMakeFiles").is_dir():
            return build, path
    return None, None


def _cache(build):
    text = (build / "CMakeCache.txt").read_text(errors="replace")
    return dict(re.findall(r"^([^#/:\n=]+):[^=\n]+=(.*)$", text, re.M))


def _link_command(build, target, cache):
    link = build / "CMakeFiles" / (target.name + ".dir") / "link.txt"
    if link.is_file():
        return link.read_text(errors="replace"), str(link)
    if not (build / "build.ninja").is_file():
        return "", None
    ninja = cache.get("CMAKE_MAKE_PROGRAM") or shutil.which("ninja")
    if not ninja:
        return "", None
    args = [ninja, "-t", "commands", "-s", str(target.relative_to(build))]
    try:
        proc = subprocess.run(args, cwd=build, capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return "", None
    return (proc.stdout, str(build / "build.ninja")) if proc.returncode == 0 else ("", None)


def _libraries(command, cache, limit):
    if not command:
        return []
    libraries = dict.fromkeys(token for token in shlex.split(command)
                              if token.startswith("/") and _LIBRARY.search(token) and Path(token).is_file())
    rows = []
    for path in libraries:
        binding = next((key for key, value in cache.items()
                        if value == path and key.endswith(("_LIBRARIES", "_LIBRARY"))), None)
        prefix = binding.rsplit("_", 1)[0] if binding else ""
        row = {"library": path}
        if binding:
            row["binding"] = binding
            if include := cache.get(prefix + "_INCLUDE_DIR"):
                row["include_dir"] = include
        rows.append(row)
    rows.sort(key=lambda row: ("binding" not in row, not row["library"].endswith(".a")))
    return rows[:limit]


def _definition(nm, path, symbol, remaining):
    args = [nm, "-g", "--defined-only", "-P"]
    if not path.endswith(".a"):
        args.append("-D")
    try:
        proc = subprocess.run([*args, path], capture_output=True, text=True, timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    for line in proc.stdout.splitlines():
        if line.split(" ", 1)[0].split("@", 1)[0] == symbol:
            return "defined"
    return "absent" if proc.returncode == 0 else "unavailable"


def inspect(run, plan, cid=None, symbol=None, limit=6):
    if symbol and not _SYMBOL.fullmatch(symbol):
        raise ValueError("link inspection requires one ELF symbol name")
    state = load(run / "experiment-state.json", {})
    requested = cid or "baseline"
    cid = requested
    candidates = state.get("candidates") or {}
    seen = set()
    while cid not in seen:
        seen.add(cid)
        info = state.get("baseline") if cid == "baseline" else candidates.get(cid)
        if not info:
            break
        build, target = _target(run, plan, cid, Path(info["workspace"]))
        if build:
            cache = _cache(build)
            command, artifact = _link_command(build, target, cache)
            if artifact:
                rows = _libraries(command, cache, max(1, min(limit, 8)))
                result = {"requested": requested, "snapshot": cid, "target": str(target),
                          "artifact": artifact, "libraries": rows}
                if symbol:
                    result["symbol"] = symbol
                    nm = shutil.which("nm")
                    deadline = time.monotonic() + 5
                    for row in rows:
                        remaining = deadline - time.monotonic()
                        row["definition"] = (_definition(nm, row["library"], symbol, remaining)
                                             if nm and remaining > 0 else "unavailable")
                return result
        if cid == "baseline":
            break
        cid = info.get("parent", "baseline")
    return {"requested": requested, "status": "link-recipe-unavailable"}
