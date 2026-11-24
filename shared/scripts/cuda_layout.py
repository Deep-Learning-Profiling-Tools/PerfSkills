"""Resolve CUPTI within the selected compiler's SDK, including NVHPC wrappers."""

from functools import lru_cache
from pathlib import Path
import re
import shutil
import subprocess


def _layout(root, compiler, source):
    cuda_include = next((p for p in (root / "targets/x86_64-linux/include", root / "include")
                         if (p / "cuda.h").is_file()), None)
    include = next((p for p in (root / "targets/x86_64-linux/include", root / "extras/CUPTI/include", root / "include")
                    if (p / "cupti_activity.h").is_file()), None)
    library = next((p for p in (root / "targets/x86_64-linux/lib", root / "extras/CUPTI/lib64", root / "lib64")
                    if (p / "libcupti.so").is_file()), None)
    if include is None or library is None or cuda_include is None:
        return None
    return {"status": "available", "reason": None, "compiler": str(compiler),
            "cuda_root": str(root), "cuda_include": str(cuda_include),
            "cupti_include": str(include), "cupti_library_dir": str(library),
            "cupti_library": str(library / "libcupti.so"), "source": source}


@lru_cache(maxsize=16)
def _resolve(compiler, stamp, explicit_root):
    compiler = Path(compiler)
    root = Path(explicit_root) if explicit_root else compiler.parent.parent
    direct = _layout(root, compiler, "explicit-root" if explicit_root else "compiler-prefix")
    if direct:
        return direct
    # NVHPC's compilers/bin/nvcc is a dispatcher, not the CUDA toolkit bin.
    # Query its selected CUDA release; do not silently choose another installation.
    siblings = root.parent / "cuda"
    if not explicit_root and siblings.is_dir():
        try:
            proc = subprocess.run([str(compiler), "--version"], capture_output=True, text=True, timeout=5)
            match = re.search(r"release\s+(\d+\.\d+)", proc.stdout) if proc.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            match = None
        if match:
            selected = _layout(siblings / match[1], compiler, "compiler-reported-sdk-release")
            if selected:
                return selected
    return {"status": "unavailable", "reason": "selected-compiler-cupti-layout-missing",
            "compiler": str(compiler), "cuda_root": str(root)}


def resolve(compiler=None, explicit_root=None):
    command = compiler or shutil.which("nvcc")
    if not command:
        return {"status": "unavailable", "reason": "cuda-compiler-unavailable"}
    path = Path(command).resolve()
    try:
        stat = path.stat()
    except OSError:
        return {"status": "unavailable", "reason": "cuda-compiler-missing", "compiler": str(path)}
    return dict(_resolve(str(path), (stat.st_size, stat.st_mtime_ns), str(explicit_root) if explicit_root else None))


def for_plan(plan=None):
    profiling = (plan or {}).get("profiling") or {}
    config = (profiling.get("providers") or {}).get("pw-cupti") or {}
    return resolve((profiling.get("tool_paths") or {}).get("nvcc"), config.get("cuda_root"))
