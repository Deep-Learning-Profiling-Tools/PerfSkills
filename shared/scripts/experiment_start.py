"""Create a measured experiment from one workload directory.

The start path loads or infers the workload contract, snapshots the baseline,
generates internal plans, then delegates cached preflight and baseline profiling
to ``experiment_state.ensure_initialized``.  It returns only the plan and the
bounded evidence files needed by the agent.
"""

from __future__ import annotations

import argparse
import math
import os
import time
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from experiment_state import ensure_initialized, read_plan
from experiment_support import REPO, display, load, write
from start_contract import experiment, preflight, substitute, tools
from start_manifest import load_manifest
from startup_provenance import record, source_comparison


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "workload"


def gpu_facts(found: dict[str, str], host_command=None) -> dict[str, str]:
    import subprocess
    proc = subprocess.run([found["nvidia-smi"], "--query-gpu=compute_cap",
                           "--format=csv,noheader"], capture_output=True, text=True)
    arch = (proc.stdout.splitlines() or [""])[0].strip().replace(".", "")
    if proc.returncode or not arch.isdigit():
        raise ValueError("could not determine CUDA compute capability")
    host_command = host_command or os.environ.get("CUDAHOSTCXX") or "g++"
    host = shutil.which(host_command)
    if not host:
        raise ValueError(f"required host compiler is unavailable: {host_command}")
    return {"cuda_arch": arch, "host_cxx": str(Path(host).resolve()), "repo": str(REPO)}


def run(argv: list[str]) -> dict:
    """Execute ``experiment.py start`` and return its compact JSON response."""
    started = time.perf_counter()
    wall_start = time.time()
    parser = argparse.ArgumentParser(prog="experiment.py start")
    parser.add_argument("--workload", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--baseline-reference", type=Path,
                        help="require declared source (including headers) to match this baseline directory")
    budget_args = parser.add_mutually_exclusive_group()
    budget_args.add_argument("--iterations", type=int, default=10)
    budget_args.add_argument("--up-to-iterations", type=int, help="allow a budget-based early finish")
    parser.add_argument("--objective", choices=["application", "kernel"], help="primary metric for source selection")
    parser.add_argument("--kernel", help="kernel objective selector, including any transformed names")
    parser.add_argument("--timing-provider", help="consistent provider for kernel objective measurements")
    parser.add_argument("--optimizer-id", help="bind evidence delivery learning to the optimizer/model version")
    parser.add_argument("--repairs", type=int, default=7)
    parser.add_argument("--policy", choices=["adaptive", "legacy"],
                        help="adaptive: one or two unprofiled executions and evidence on demand; legacy: manifest policy")
    args = parser.parse_args(argv)
    if args.objective == "kernel" and not args.kernel:
        raise ValueError("--objective kernel requires --kernel REGEX")
    if args.up_to_iterations is not None:
        args.iterations = args.up_to_iterations
    if args.iterations < 1 or args.repairs < 1:
        raise ValueError("iterations and repairs must be positive")
    # 1. Resolve the workload contract and create an immutable baseline snapshot.
    manifest, manifest_path, source = load_manifest(args.workload)
    if args.baseline_reference:
        reference = args.baseline_reference.resolve()
        if not reference.is_dir():
            raise ValueError("baseline reference must be an existing source directory")
        comparison = source_comparison(source, reference, manifest["fingerprint_globs"], manifest.get("copy_ignore"))
        if not comparison["same"]:
            raise ValueError(f"baseline source differs from reference: missing={comparison['missing'][:8]}, "
                             f"added={comparison['added'][:8]}, changed={comparison['changed'][:8]}")
    run_id = args.run_id or (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") +
                             slug(manifest["name"]))
    run_dir = REPO / "runs" / run_id
    prior_contract = load(run_dir / "start-contract.json", {})
    policy = args.policy or (prior_contract.get("policy", "legacy") if prior_contract else "adaptive")
    if args.up_to_iterations is not None and policy != "adaptive":
        raise ValueError("a flexible iteration budget requires the adaptive policy")
    baseline = run_dir / "baseline-source"
    if args.baseline_reference and baseline.exists():
        if not source_comparison(baseline, reference, manifest["fingerprint_globs"], manifest.get("copy_ignore"))["same"]:
            raise ValueError("existing baseline snapshot differs from the requested reference")
    if run_dir.exists() and not (run_dir / "start-contract.json").is_file():
        raise ValueError(f"run directory already exists and is not resumable: {display(run_dir)}")
    if not baseline.exists():
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, baseline, ignore=shutil.ignore_patterns(
            *manifest.get("copy_ignore", [])), dirs_exist_ok=True)
    if prior_contract and (run_dir / "experiment-plan.json").is_file():
        # A new policy default/output adapter must never rewrite a resumed plan.
        if args.policy and args.policy != prior_contract.get("policy", "legacy"):
            raise ValueError("an existing run retains its original policy; use a new run id")
        plan, run_dir, variables = read_plan(run_dir / "experiment-plan.json")
        from acquisition_cost import event
        try:
            from resource_meter import measure
            with measure(plan, run_dir, "command", "resume"):
                state, _ = ensure_initialized(plan, run_dir, variables)
        finally:
            event(run_dir, "initialize", time.perf_counter() - started, resumed=True)
        from objective_acquisition import initialize
        initialize(plan, run_dir, state, args)
        return initialized_response(plan, state, run_id, run_dir)
    # 2. Resolve tools once and generate the controller-owned plans.
    found = tools((manifest.get("profiling") or {}).get("tool_paths"))
    facts = gpu_facts(found, (manifest.get("env") or {}).get("CUDAHOSTCXX"))
    resolved = substitute(manifest, facts | {"nvcc": found["nvcc"]})
    preflight_plan = preflight(run_id, run_dir, source, found)
    plan = experiment(resolved, run_id, run_dir, baseline, found,
                      args.iterations, args.repairs, policy=policy)
    if args.objective:
        plan["selection_objective"] = (plan.get("selection_objective") or {}) | {"scope":args.objective}
    if policy == "adaptive":
        from resource_meter import validate
        validate(plan)
        if args.up_to_iterations is not None:
            from rewrite_evidence import enabled as lookahead
            if not lookahead(plan):
                raise ValueError("a flexible budget requires the lookahead decision policy")
        if args.up_to_iterations is not None:
            plan["budget"]["flexible"] = True
        settings = plan["profiling"]["skill_runtime"]
        if args.optimizer_id or os.environ.get("PROFILEWEAVER_OPTIMIZER_ID"):
            settings["optimizer_id"] = args.optimizer_id or os.environ["PROFILEWEAVER_OPTIMIZER_ID"]
        wall_budget = settings.get("campaign_wall_budget_s")
        if wall_budget is not None:
            if (isinstance(wall_budget, bool) or not isinstance(wall_budget, (int, float))
                    or not math.isfinite(wall_budget) or wall_budget <= 0):
                raise ValueError("campaign_wall_budget_s must be positive and finite")
            settings["campaign_deadline"] = wall_start + wall_budget
        # Bind compiler discovery and child build/capture commands to the same tool.
        plan["profiling"]["tool_paths"]["nvcc"] = found["nvcc"]
        plan["env"]["PATH"] = str(Path(found["nvcc"]).parent) + os.pathsep + plan["env"].get("PATH", os.environ.get("PATH", ""))
        from metric_output import configure as configure_output
        plan = configure_output(plan, run_dir)
        record(run_dir, baseline, plan, found, facts)
    write(run_dir / "preflight-plan.json", preflight_plan)
    write(run_dir / "experiment-plan.json", plan)
    write(run_dir / "start-contract.json", {"protocol": "profileweaver.start/v1",
          "manifest": str(manifest_path) if manifest_path else None,
          "source": str(source), "iterations": args.iterations,
          "repairs": args.repairs, "policy": policy})
    # 3. Build, validate, profile, reduce, and locate bounded source ranges.
    plan, run_dir, variables = read_plan(run_dir / "experiment-plan.json")
    from acquisition_cost import event
    try:
        from resource_meter import measure
        with measure(plan, run_dir, "command", "initialize"):
            state, _ = ensure_initialized(plan, run_dir, variables)
    finally:
        event(run_dir, "initialize", time.perf_counter() - started)
    # 4. Expose compact evidence paths; detailed profiler output remains under raw/.
    from objective_acquisition import initialize
    initialize(plan, run_dir, state, args)
    return initialized_response(plan, state, run_id, run_dir)


def initialized_response(plan: dict, state: dict, run_id: str, run_dir: Path) -> dict:
    from cupti_evidence import baseline as event_baseline
    from evidence_presentation import baseline_packet
    from selection_objective import notice
    ranking = notice(state)
    metric = plan.get("metric")
    if not ranking and metric:
        sample = load(run_dir / "baseline-timing.json", {}).get("sample") or {}
        ranking = {"objective": {"scope": "application", "measurement": "unprofiled",
                                 "metric": metric.get("name"), "unit": metric.get("unit"),
                                 "direction": metric.get("direction"), "baseline": sample.get("metric")}}
    return {"status": "initialized", "run_id": run_id, **ranking,
            **event_baseline(plan, run_dir, baseline_packet(run_dir, state)),
            "controller": display(run_dir / "experiment-plan.json"),
            "profile": display(run_dir / "agent-brief.txt"),
            "source": display(run_dir / "source-targets.json"),
            "read_only": [display(run_dir / "agent-brief.txt"),
                          display(run_dir / "source-slices")],
            "edit_only": display(run_dir / "candidates/<id>/source"),
            "kernel": (((state.get("kernel_profiles") or {}).get("baseline") or state.get("baseline_activity") or {}).get("kernel"))}
