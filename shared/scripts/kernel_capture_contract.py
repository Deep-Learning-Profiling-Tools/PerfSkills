"""Kernel capture identity, paths and immutable evidence contracts."""

import hashlib
import json
from pathlib import Path
from experiment_support import REPO, candidate_dir, evidence_path_state, expand, resolve, sha256, source_state
from profile_policy import ncu_sections

CAPTURE = REPO / ".claude/skills/pw-nsight-compute/scripts/capture.py"


def kernel_pattern(plan: dict, phase: str) -> str:
    config, attribution = plan.get("kernel_profile") or {}, plan.get("attribution") or {}
    key = "baseline_kernel_regex" if phase == "baseline" else "variant_kernel_regex"
    fallback = "baseline_target_regex" if phase == "baseline" else "variant_target_regex"
    return config.get(key) or attribution.get(fallback)


def locations(run: Path, candidate_id: str):
    if candidate_id == "baseline":
        return (run / "raw/ncu/baseline", run / "profile.ncu.baseline.json",
                run / "kernel-diagnostics.json", run / "ncu-drilldowns", run / "ncu-cache.json")
    if candidate_id == "final":
        root = run / "ncu-final"
        return (run / "raw/ncu/final", root / "profile.ncu.reprofile.json",
                root / "kernel-diagnostics.json", root / "drilldowns", root / "ncu-cache.json")
    root = candidate_dir(run, candidate_id) / "profile"
    return (run / "raw/ncu" / candidate_id, root / "profile.ncu.variant.json",
            root / "kernel-diagnostics.json", root / "ncu-drilldowns", root / "ncu-cache.json")


def evidence_fingerprint(plan: dict, run: Path, candidate_id: str,
                         workspace: Path, diagnostic: dict | None = None) -> tuple[str, list[dict]]:
    values = {"repo": str(REPO), "run_dir": str(run), "run_id": plan["run_id"],
              "workspace": str(workspace), "id": candidate_id}
    config = plan.get("kernel_profile") or {}
    states = [evidence_path_state(resolve(value, values), workspace)
              for value in config.get("fingerprint_inputs") or []]
    missing = [item["path"] for item in states if item.get("state") == "missing"]
    if missing:
        raise ValueError("kernel_profile fingerprint input missing: " + ",".join(missing))
    if not states:
        command0 = Path(expand(plan["execution"]["command"][0], values))
        cwd = resolve(plan["execution"].get("cwd", "{workspace}"), values)
        executable = command0 if command0.is_absolute() else cwd / command0
        try:
            local = executable.resolve().is_relative_to(workspace.resolve())
        except (OSError, ValueError):
            local = False
        states = ([evidence_path_state(executable, workspace)] if local and executable.is_file()
                  else [{"path": "declared-source", "state": "source-tree",
                         "files": source_state(workspace, plan["source"]["fingerprint_globs"])}])
    material = {"normalization_revision": 2, "inputs": states, "kernel": kernel_pattern(plan, "variant"),
                "sections": ncu_sections(config, "variant", diagnostic=diagnostic)}
    if config.get("kernel_name_base") == "mangled":
        material["kernel_name_base"] = "mangled"
    digest = hashlib.sha256(json.dumps(material, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return digest, states


def _signature(plan: dict, workspace: Path, timeline: Path, phase: str,
               candidate_id: str, force: bool, diagnostic: dict | None = None) -> str:
    material = {"config": plan.get("kernel_profile"), "phase": phase,
                "candidate": candidate_id, "force": force,
                "command": plan.get("execution", {}).get("command"),
                "source": source_state(workspace, plan["source"]["fingerprint_globs"]),
                "timeline": sha256(timeline),
                "sections": ncu_sections(plan.get("kernel_profile") or {}, phase, force,
                                         diagnostic),
                "ncu_scripts": source_state(CAPTURE.parent, ["*.py"])}
    return hashlib.sha256(json.dumps(material, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()

