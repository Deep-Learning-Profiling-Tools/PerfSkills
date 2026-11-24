"""Current CUPTI PC sampling without rebuilding or editing the application.

The SDK samples supply collection and decoding; their original source is never
modified. A cached copy fixes only output naming so profiles stay under raw/.
PC samples explain observed stalls, not GPA's causal/dataflow attribution.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import uuid

from campaign_io import check_oracle, extract
from experiment_support import REPO, display, expand, expanded_env, load, resolve, sha256, write
from validate import errors_for
from acquisition_limits import bounded, remaining
from bounded_process import run as run_process

CAPABILITIES = {"gpu-pc", "instruction-stalls"}
_PROBE = r'''
#include <cuda.h>
#include <cupti_pcsampling.h>
#include <cstdio>
int main() {
  CUdevice device; CUcontext context;
  CUresult cuda = cuInit(0);
  if (cuda != CUDA_SUCCESS || cuDeviceGet(&device, 0) != CUDA_SUCCESS ||
      cuDevicePrimaryCtxRetain(&context, device) != CUDA_SUCCESS) return 2;
  cuCtxSetCurrent(context);
  CUpti_PCSamplingEnableParams enable = {};
  enable.size = CUpti_PCSamplingEnableParamsSize; enable.ctx = context;
  CUptiResult result = cuptiPCSamplingEnable(&enable);
  const char *name = nullptr; cuptiGetResultString(result, &name);
  std::printf("%s\n", name ? name : "unknown");
  if (result == CUPTI_SUCCESS) {
    CUpti_PCSamplingDisableParams disable = {};
    disable.size = CUpti_PCSamplingDisableParamsSize; disable.ctx = context;
    cuptiPCSamplingDisable(&disable);
  }
  cuDevicePrimaryCtxRelease(device);
  return result == CUPTI_SUCCESS ? 0 : 3;
}
'''


def _stage(command, cwd, env, raw, name, timeout):
    started = time.perf_counter()
    stdout, stderr = raw / (name + ".stdout.txt"), raw / (name + ".stderr.txt")
    try:
        proc = run_process(command, cwd, env, stdout, stderr, remaining(timeout))
        code = proc.returncode
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        with stderr.open("a") as stream:
            stream.write("\nProfileWeaver: " + str(exc) + "\n")
        code = None
    return {"command": command, "cwd": str(cwd), "returncode": code,
            "elapsed_s": time.perf_counter() - started, "stdout": str(stdout), "stderr": str(stderr)}


def probe(plan: dict) -> dict:
    if os.environ.get("CUDA_INJECTION64_PATH") or os.environ.get("INJECTION_PARAM"):
        return {"status": "unavailable", "reason": "existing-injection-conflict", "capabilities": []}
    config = (((plan.get("profiling") or {}).get("providers") or {}).get("pw-cupti") or {})
    nvcc, cxx = shutil.which("nvcc"), shutil.which("g++")
    if not nvcc or not cxx or not shutil.which("c++filt"):
        return {"status": "unavailable", "reason": "compiler-unavailable", "capabilities": []}
    from cuda_layout import for_plan
    layout = for_plan(plan)
    if layout["status"] != "available":
        return {"status": "unavailable", "reason": layout["reason"], "capabilities": []}
    cuda = Path(layout["cuda_root"])
    sample = cuda / "extras/CUPTI/samples"
    source = sample / "pc_sampling_continuous/pc_sampling_continuous.cpp"
    utility = sample / "pc_sampling_utility/pc_sampling_utility.cpp"
    helper = utility.with_name("pc_sampling_utility_helper.h")
    include, library = Path(layout["cupti_include"]), Path(layout["cupti_library_dir"])
    inputs = [source, utility, helper, include / "cupti_pcsampling.h", library / "libcupti.so", library / "libpcsamplingutil.so"]
    if any(not p.is_file() for p in inputs):
        return {"status": "unavailable", "reason": "current-cupti-sdk-samples-or-libraries-missing", "capabilities": []}
    env = os.environ.copy() | {str(k): str(v) for k, v in (plan.get("env") or {}).items()}
    env.update({str(k): str(v) for k, v in (plan.get("execution", {}).get("env") or {}).items()})
    try:
        device = subprocess.run(["nvidia-smi", "--query-gpu=uuid,driver_version,compute_cap", "--format=csv,noheader"],
                                capture_output=True, timeout=remaining(5), env=env)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable", "reason": "device-probe-failed", "capabilities": []}
    signature = {"revision": 1, "adapter": sha256(Path(__file__)),
                 "inputs": [[str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns] for p in inputs],
                 "compiler": [str(Path(cxx).resolve()), Path(cxx).stat().st_mtime_ns],
                 "device": device.stdout.decode(errors="replace"),
                 "env": {k: env.get(k) for k in ("CUDA_VISIBLE_DEVICES", "LD_LIBRARY_PATH")}}
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:24]
    root = REPO / "runs/_cache/cupti-pc" / key
    cached = load(root / "probe.json", {})
    if cached:
        return cached | {"cache": "hit"}
    if (plan.get("profiling") or {}).get("probe_cache_only"):
        return {"status": "unavailable", "reason": "capability-probe-not-cached-request-provider-explicitly",
                "capabilities": [], "artifact": str(root), "probe_s": 0}
    root.mkdir(parents=True, exist_ok=True)
    text = source.read_text()
    original = 'std::to_string((long int)pContextInfo->contextUid) + "_" + g_fileName'
    if text.count(original) != 1:
        return {"status": "unavailable", "reason": "sdk-output-naming-pattern-unsupported", "capabilities": []}
    # SDK prefixes the entire filename with context ID, breaking absolute paths.
    # Add the ID as a suffix in the cached build; leave installed SDK untouched.
    modified = root / "pc_sampling_continuous.cpp"
    modified.write_text('#include <unistd.h>\n' + text.replace(original,
        'g_fileName + "." + std::to_string((long int)getpid()) + "." + std::to_string((long int)pContextInfo->contextUid)'))
    (root / "probe.cpp").write_text(_PROBE)
    flags = [cxx, "-std=c++17", "-O2", "-pthread", "-I" + str(include), "-I" + layout["cuda_include"],
             "-I" + str(sample / "common"), "-L" + str(library), "-Wl,-rpath," + str(library)]
    libs = ["-lcupti", "-lpcsamplingutil", "-lcuda"]
    commands = [("build-collector", flags + ["-shared", "-fPIC", str(modified), *libs, "-o", str(root / "pc.so")]),
                ("build-decoder", flags + [str(utility), *libs, "-o", str(root / "decode")]),
                ("build-probe", flags + [str(root / "probe.cpp"), *libs, "-o", str(root / "device-probe")])]
    stages = []
    for name, argv in commands:
        stage = _stage(argv, root, env, root, name, 60); stages.append(stage)
        if stage["returncode"] != 0:
            break
    if stages[-1]["returncode"] == 0:
        stages.append(_stage([str(root / "device-probe")], root, env, root, "device-probe", 20))
    valid = len(stages) == 4 and all(s["returncode"] == 0 for s in stages)
    result = {"status": "available" if valid else "unavailable", "capabilities": sorted(CAPABILITIES) if valid else [],
              "reason": None if valid else "pc-api-build-or-device-probe-failed", "artifact": str(root),
              "collector": str(root / "pc.so"), "decoder": str(root / "decode"),
              "sdk_source": str(source), "sdk_source_sha256": sha256(source), "stages": stages,
              "version": str(cuda.resolve()), "probe_s": sum(s["elapsed_s"] for s in stages)}
    write(root / "probe.json", result)
    return result

