# Automated optimization controller

This is a maintainer reference for the generated `experiment-plan.json`. Routine agents invoke
`experiment.py start --workload <directory>` and never read or edit this contract.

## Ownership boundary

An optional workload `profileweaver.json` supplies non-default setup facts. When it is absent,
`start` derives a conservative contract from standard build files and the executable. In either
case, `start` turns the contract into internal preflight and experiment plans. `experiment.py` owns:
The inferred command uses a standard `run` target when present; it never invents benchmark
arguments. A workload requiring custom arguments or a semantic oracle must provide those facts
through its optional contract.

- schema validation and phase-specific profiler routing;
- cached preflight, baseline build, baseline capture, normalization, and source mapping;
- candidate workspaces and patches;
- correctness-aware paired timing and early stopping;
- per-candidate profiling, cache/reuse decisions, and baseline/parent deltas;
- bounded hotspot, callee, data declaration, writer, allocation, and transfer slices;
- zero-run evidence promotion, optional fresh profiling for unresolved gates, reports, and state.

The agent must not create helper Python, shell, awk, or jq programs or invoke backend modules.
Full stdout, stderr, captures, counters, and samples remain under `raw/`.

## Core plan

All commands are argument arrays and never use an implicit shell. Paths may use `{repo}`, `{run_id}`,
`{run_dir}`, `{workspace}`, `{id}`, `{profile_dir}`, `{profile_raw}`, `{profile_output}`, and
`{baseline_profile}`.

```json
{
  "version": 1,
  "run_id": "example-run",
  "run_dir": "runs/example-run",
  "question": "Reduce the measured GPU lookup phase without changing results",
  "env": {"CUDA_VISIBLE_DEVICES": "0"},
  "preflight_plan": "runs/example-run/preflight-plan.json",
  "profiling": {
    "discovery_needs": ["gpu-hotspot", "transfer", "sync"],
    "diagnostic_needs": ["memory-latency", "instruction-stalls", "register-pressure"]
  },
  "source": {
    "baseline_dir": "workloads/example",
    "fingerprint_globs": ["**/*.cu", "**/*.cuh", "Makefile"]
  },
  "execution": {
    "build": ["make", "-j4"],
    "build_cwd": "{workspace}",
    "command": ["{workspace}/app", "input.deck"],
    "cwd": "{workspace}"
  },
  "metric": {
    "name": "lookup CUDA-event time",
    "source": "stdout",
    "regex": "LOOKUP_MS=([0-9.]+)",
    "group": 1,
    "unit": "ms",
    "direction": "lower"
  },
  "kernel_timing": {
    "source": "cuda-event",
    "detail": "Events bracket the stable lookup ROI; whole-process wall time remains collateral"
  },
  "oracle": {"type": "stdout_regex", "regex": "Verification: PASS"},
  "sampling": {
    "warmup": 1,
    "min_pairs": 5,
    "max_pairs": 10,
    "final_min_pairs": 5,
    "final_max_pairs": 15,
    "finalists": 2,
    "bootstrap_iterations": 2000,
    "practical_threshold": 1.02,
    "wall_gate": "advisory"
  },
  "attribution": {
    "baseline_target_regex": "lookup_kernel",
    "variant_target_regex": "lookup_kernel",
    "metric": "self_time_s",
    "direction": "lower"
  },
  "iteration_profile": {
    "baseline_profile": "{run_dir}/profile.baseline.json",
    "profile_output": "{profile_dir}/profile.json",
    "source_targets": "{profile_dir}/source-targets.json",
    "cadence": "baseline-final",
    "max_delta_items": 2,
    "stages": [
      {
        "id": "capture",
        "argv": ["python3", "<timing-child-wrapper>", "--workspace", "{workspace}",
                 "--raw", "{profile_raw}", "--output", "{profile_output}"],
        "fingerprint_inputs": ["<timing-child-wrapper>"],
        "produces": ["{profile_output}"]
      },
      {
        "id": "locate",
        "argv": ["python3", "shared/scripts/locate_source.py", "--profile",
                 "{profile_output}", "--run-dir", "{profile_dir}", "--source-root",
                 "{workspace}", "--repo-root", "{repo}", "--output",
                 "{profile_dir}/source-targets.json", "--max-variants", "3",
                 "--dependency-depth", "2",
                 "--max-dependencies", "3"],
        "produces": ["{profile_dir}/source-targets.json"]
      }
    ]
  },
  "kernel_profile": {
    "enabled": true,
    "section": "all",
    "candidate_sections": "auto",
    "available_sections": ["SpeedOfLight", "LaunchStats", "Occupancy", "SchedulerStats",
                           "WarpStateStats", "MemoryWorkloadAnalysis", "SourceCounters"],
    "baseline_kernel_regex": "lookup_kernel",
    "variant_kernel_regex": "lookup_kernel",
    "launch_count": 1,
    "timeout_s": 900,
    "tool_version": "<cached-preflight-version>",
    "fingerprint_inputs": ["{workspace}/app"]
  }
}
```

The preflight plan records exact compiler, device, profiler version/capability/permission, build,
launch, and oracle facts once. It must extract installed NCU section names before placing them in
`available_sections`. Never guess a section or metric name. `init` runs preflight through its cache;
matching tool paths, environment, and declared inputs do not repeat verification.

If the baseline normalized profile is absent, `init` builds the baseline, runs
`iteration_profile.stages` with `id=baseline`, validates the profile, and requires source targets.
If it exists and fingerprints match, it is reused. Expensive capture and cheap normalization should
be separate stages when a child can replay a raw artifact.

## Routing

`profiling.discovery_needs` and `profiling.diagnostic_needs` are resolved independently by maximum
capability coverage, then availability, then cost. A normal NVIDIA kernel journey selects NSYS for
discovery and NCU for internals. CPU hotspots select perf; source/calling context can select
HPCToolkit; supported instruction-sampling optimization can select GPA; supplied redundancy or
memory-liveness artifacts can select GVProf or DrGPUM. NCU is not enabled by CUDA alone.

The selected routes and probes are written to `profiler-routing.json`. If `kernel_profile.enabled`
conflicts with a different selected diagnostic child, initialization fails instead of silently
running NCU.

## GPU cadence

`iteration_profile.cadence: baseline-final` is the default. It means:

1. The timing child captures the baseline timeline and absolute hotspot cost.
2. Baseline NCU collects `section=all` for one measured kernel.
3. Unprofiled candidate timing uses `metric`; a CUDA-event or application ROI metric is preferred
   for kernel optimization. Wall time is recorded separately for collateral effects.
4. In standard mode, every correct changed kernel gets filtered NCU evidence. An unchanged
   kernel-evidence fingerprint gets explicit zero-cost reuse. A workload using
   `sampling.mode: winner-confirmation` with `profiling.cadence: every-candidate` returns paired
   timing and NSYS timeline deltas during screening while deferring candidate NCU.
5. The accepted winner reuses its paired timing and NSYS evidence during screening. The final
   winner campaign runs the configured multi-pair confirmation and restores full NSYS and NCU.

Set `cadence: every-candidate` when timeline deltas can change the next decision, such as
transfers, launches, overlap, or synchronization. Keep `baseline-final` for an isolated kernel
whose internal cause is already covered by NCU.

When `candidate_sections` is `auto`, the baseline/parent diagnosis chooses a verified tier:

- memory transaction waste/latency: launch, occupancy, scheduler, warp state, memory, source;
- bandwidth: speed of light, launch, occupancy, scheduler, memory, source;
- compute: speed of light, launch, occupancy, scheduler, warp state, compute, instructions;
- register spill: launch, occupancy, scheduler, warp state, memory, source;
- resource/issue starvation: launch, occupancy, scheduler, warp state;
- divergence: launch, scheduler, warp state, instructions, source.

The intersection with `available_sections` is used. An empty intersection falls back to full
evidence. Baseline uses full evidence. `kernel_profile.nvtx_include` may further
bound an existing verified NVTX phase; it does not replace kernel filtering or timing.

## Compact evidence and drill-downs

The candidate result reports capture/reuse reason, NCU profiling seconds, kernel duration,
registers, occupancy, eligibility/issue, leading stall, load bytes per sector, DRAM bytes, primary
diagnosis, structural requirement, ranked mechanisms, and the largest baseline/parent deltas.
Full counters never enter context.

Named NCU groups are launch, occupancy, scheduler, stalls, instructions, memory, throughput,
roofline, rules, and source. Load one through the public CLI only when it can change the next edit:

```bash
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json inspect \
  --section ncu --candidate <id> --group memory
```

## Search and models

`prepare-batch` accepts exactly one candidate per step so the returned evidence stays compact.
That is an execution-shaping choice, not a search restriction: the agent supplies the mechanism,
description, parent, and all linked source edits. ProfileWeaver measures the candidate and returns
a compact delta; it never rejects a candidate because a sibling, structural family, or parameter
value was previously tried. Compatible mechanisms may be stacked, and the agent may branch from
any preserved parent. Use `PARENT=current` to resolve the currently measured best candidate; the
first `current` step resolves to `baseline`.

The advisor attaches measured-input models for transaction efficiency, latency hiding, observed
traffic rate, and Amdahl ceiling. These are descriptive evidence and analytical bounds, never
candidate generation, search ordering, action hints, or speedup predictions. Future models may add transfer
bounds, static search/work counts, resource feasibility, and empirical residuals learned from
comparable candidates. Only measurements establish speedup.

## Confirmation and learning

Standard screening stops after the minimum five pairs when the confidence interval establishes
meaningful improvement, regression, or practical equivalence. A
`winner-confirmation` workload uses its configured one-pair screening pass only to rank candidates;
after the iteration budget the controller runs the final multi-pair campaign for the best candidate
and no runner-up. Confirmation promotes the strongest candidate from the resulting paired timing
and NCU evidence. `confirm --fresh` remains reserved for an unresolved attribution or
collateral-cost gate in standard mode.

Within a run, state advances the measured hotspot frontier and preserves the globally fastest
kernel candidate even if a later batch regresses. Across runs, three comparable outcomes are
required before a cost or mechanism prior becomes eligible. Priors may break capability/cost ties
or order hypotheses. They never override availability, current evidence, correctness, statistical
gates, or source.
