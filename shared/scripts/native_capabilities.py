"""Parse positive native GPU capabilities and architecture support."""

import hashlib
import re
from pathlib import Path


def advertised_events(text: str) -> dict:
    coarse = re.search(r"^\s*(gpu=(?:cuda|nvidia))\s{2,}", text, re.MULTILINE)
    pc = re.search(r"^\s*(gpu=(?:cuda|nvidia),pc)\s{2,}", text, re.MULTILINE)
    # Some supported builds describe the PC option in prose. Require an explicit
    # option plus affirmative support, and reject CUDA-version incompatibility.
    negative = re.search(r"[^\n]*(?:,pc|PC sampl)[^\n]*(?:not supported|unsupported|unavailable|disabled)", text, re.I)
    positive = re.search(r"gpu=(cuda|nvidia),pc[^\n]*(?:collect|enable|sampl)", text, re.I)
    return {"coarse_event": coarse.group(1) if coarse else None,
            "pc_event": ((pc.group(1) if pc else "gpu=" + positive.group(1) + ",pc" if positive else None)
                         if not negative else None),
            "pc_limitation": negative.group(0).strip() if negative else None}


def gpa_models(config: dict, help_text: str) -> dict:
    """Verify models rather than relying on --gpu-arch accepting any string.

    The upstream analyzer silently chooses V100 for every non-A100 name. An
    explicit name alone therefore cannot establish architecture support.
    """
    source = Path(config["source_root"]) / "src/lib/analysis/advisor/GPUArchitecture.hpp" if config.get("source_root") else None
    if source and source.is_file():
        models = re.findall(r"class\s+(\w+)\s*:\s*public\s+GPUArchitecture", source.read_text(errors="replace"))
        init = source.with_name("GPUAdvisor-Init.cpp")
        if not init.is_file():
            return {"models": [], "proof": "model-initializer-missing"}
        initializer = init.read_text(errors="replace")
        names = re.findall(r'gpu_arch\s*==\s*"([A-Za-z0-9_-]+)"', initializer)
        names += re.findall(r'else\s*\{\s*this->_arch\s*=\s*new\s+(\w+)\(', initializer)
        return {"models": sorted(set(models) & set(names)), "proof": [str(source), str(init)],
                "sha256": hashlib.sha256(source.read_bytes() + init.read_bytes()).hexdigest()}
    match = re.search(r"Supported GPU (?:architectures|models)\s*:\s*([^\n]+)", help_text, re.I)
    return {"models": re.findall(r"[A-Za-z0-9_-]+", match.group(1)) if match else [],
            "proof": "installed-hpcprof-help" if match else None}


def model_matches_devices(model: str, devices: list[str]) -> bool:
    return bool(devices) and all(re.search(r"(?<![A-Za-z0-9])" + re.escape(model) + r"(?![A-Za-z0-9])", x, re.I)
                                 for x in devices)

