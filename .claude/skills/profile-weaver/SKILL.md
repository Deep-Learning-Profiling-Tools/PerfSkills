---
name: profile-weaver
description: Route an HPC performance question to the cheapest capable profiler, then produce a measured, correctness-checked optimization. Use for profiling, optimization, regressions, profiler selection, and evidence-backed rewrites.
---

# ProfileWeaver

Return a rewrite that passes correctness, significance, mechanism, collateral-cost,
attribution, and reproducibility gates. Never invent measurements, supported flags, architecture,
or causal explanations. Unknown values remain `null`.

## One public entry point

For an optimization journey, invoke only:

```bash
python3 shared/scripts/experiment.py start --workload <directory> --iterations 10 --repairs 7
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json prepare-batch \
  --candidate a:baseline:<mechanism>
# edit only the returned candidate workspace and source slices
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json evaluate a
# after the verdict, use PARENT=current to inherit the measured best candidate
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json prepare-batch \
  --candidate b:current:<next-mechanism>
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json confirm a
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json status
```

`start` discovers and caches compiler, GPU, NSYS, NCU version and sections; creates both internal
contracts; copies and builds the baseline; checks correctness; runs baseline NSYS; selects the
measured kernel; runs baseline NCU; reduces both profiles; and emits bounded source slices. Never
author or edit `experiment-plan.json`, `preflight-plan.json`, or framework Python during a journey.

The optimizer may read only the returned `agent-brief.txt`, explicitly named profile drill-downs,
and emitted source slices. It may edit only source under a returned candidate workspace. Do not
read README files, schemas, controller state, backend Python, raw profiler output, full logs, or
other benchmark files. Do not open child `pw-*` skill files; the controller owns their mechanics.
When `source-targets.json` exposes `existing-variant` slices, treat them as bounded search-space
references: compare their structural mechanism with the measured baseline before inventing a
new local or parameter change. They are source context, not performance evidence; correctness
and the declared metric still decide.
GPA-style symptom families and Roofline/Amdahl values in the brief are descriptive measurements.
The agent packet contains no generated mechanism labels, action hints, priorities, or opportunity
rankings. Keep workload names only in the required kernel and source-slice identifiers; do not
turn a field name or a benchmark-specific algorithm into a framework hypothesis. Descriptions
passed with `prepare` are agent-provided audit text, not generated advice.
A controller failure is reported from its compact error plus named raw
artifact; it is not repaired by changing the framework during the optimization task.

Use `inspect --section <name>` only when the compact profile names missing evidence that can
change the next decision. For NCU, add `--candidate <id> --group <name>`. The controller invokes
routing, profilers, reducers, source location, sampling, correctness, caching, delta generation,
confirmation, reporting, and learning internally. Do not invoke `route.py`, child capture
scripts, `campaign.py`, `brief.py`, `agent_view.py`, or `finalize.py` during a routine journey.

For long-running workloads, a manifest may set `sampling.mode` to `winner-confirmation`, with
`screen_min_pairs`/`screen_max_pairs` normally set to `1` and `profiling.cadence` set to
`every-candidate`. Screening then performs one paired baseline/candidate execution and captures an
NSYS timeline delta per iteration while deferring candidate NCU replay. Once the iteration budget
is exhausted, the controller runs the final multi-pair campaign and full profiling for the best
measured candidate only. Standard workloads retain the five-pair screening default.

## Profiler selection

Declare the question as capability tags under `profiling.discovery_needs` and
`profiling.diagnostic_needs`. `init` selects the cheapest available child for each phase and
writes `profiler-routing.json`.

- `pw-perf`: CPU hotspots, hardware counters, cache, branches, vectorization.
- `pw-nsight-systems`: GPU/CPU timeline, absolute kernel cost, launches, transfers,
  synchronization, overlap, NVTX phases.
- `pw-nsight-compute`: internals of one already measured NVIDIA kernel: stalls, scheduler,
  registers, occupancy limiters, memory transactions, cache, instructions, throughput, roofline.
- `pw-hpctoolkit`: calling context and source attribution across CPU/GPU work.
- `pw-gpa`: instruction/source-line stall attribution and GPA optimization advice on supported
  workloads.
- `pw-gvprof`: pre-captured GPU value redundancy; pair with measured time.
- `pw-drgpum`: pre-captured GPU memory liveness and capacity, never runtime.

A CUDA executable does not imply NCU. Use NCU only when an expensive kernel is known and its
internal mechanism is still unresolved. A blocked or partial route is evidence; do not silently
substitute another tool.

## GPU evidence ladder

1. Run cached preflight once for compiler, device, profiler capability, permissions, build,
   launch, and oracle. Reuse it.
2. Use one baseline NSYS or equivalent timing capture to rank kernels and expose timeline costs.
   Use NVTX to delimit a stable phase when the application already provides ranges.
3. Rank unprofiled candidates with the declared metric. Prefer a high-resolution CUDA-event or
   application ROI time emitted on stdout for a kernel objective; retain whole-process time as a
   collateral measurement. NCU replay duration is evidence, never benchmark time.
4. Capture baseline NCU `section=all` for the one bounded kernel. The controller reduces the
   full counter table into a small hierarchical diagnosis.
5. Every correct candidate in standard mode receives an NCU disposition and parent- plus
   baseline-relative delta. In `winner-confirmation` screening with `every-candidate` cadence,
   return paired timing and NSYS timeline deltas while deferring candidate NCU; the selected
   winner receives full NCU evidence during final confirmation. A changed kernel is captured; an identical kernel fingerprint explicitly reuses
   evidence. When cached preflight supplied verified section names, candidate replay uses the
   section tier for the current diagnosis. Otherwise it falls back to full evidence rather than
   guessing.
6. Reuse the accepted winner's paired benchmark and candidate NCU delta. Use `confirm --fresh`
   only when attribution or collateral evidence is unresolved.

Use `every-candidate` NSYS only when timeline deltas can change the next decision, such as
transfers, launches, overlap, or synchronization. Keep candidate NCU deferred in long-running
screening. Do not run NCU on every kernel. Use `kernel_profile.nvtx_include` only with a verified
existing NVTX range.

## Evidence hierarchy

Reason from broad cost to narrow cause:

1. **Application:** end-to-end/ROI time, GPU busy time, transfers, launches, synchronization.
2. **Kernel:** duration and frequency; grid/block shape; absolute DRAM bytes.
3. **Function/data:** bounded hotspot, callees, accessed objects, declarations, builders/writers,
   and transfers selected by the dependency locator.
4. **Instruction:** leading warp stalls, issue eligibility, instruction mix, branch efficiency,
   source/SASS evidence, cache/sector waste.

The diagnostic reducer jointly evaluates register footprint and spills, achieved/theoretical
occupancy and resource limiters, active/eligible/issued warps, issue activity, stalls, SM/DRAM/
L1/L2 throughput, hit rates, bytes per sector, DRAM bytes, instructions, branches, shared-memory
conflicts, and profiler rules. Missing metrics stay absent. Read one named metric group only when
the compact hierarchy cannot distinguish the next action.

Analytical models provide bounds and priorities, never claimed speedups. Apply the GPA-style
symptom family first, then measured Roofline resource classification, a kernel opportunity
ceiling, and Amdahl translation through the timing profiler's hotspot fraction. Also consider
memory-transaction efficiency, traffic rate, latency hiding, resource feasibility, transfer
bounds, and static work/search-depth or footprint changes. The candidate still has to win
measurement and correctness gates.

## Optimization search

Work on the largest measured remaining cost. Prefer structural mechanisms before launch-parameter
tuning: remove work or traffic, change a search/index, transform data layout/order, preserve data
residency, group similar work, fuse/fission phases, or precompute invariants. Then consider
coalescing, reuse, instruction mix, live ranges, block shape, and occupancy.

Treat the brief's GPA-style symptom family, counters, deltas, and source/dataflow slices as
descriptive evidence. ProfileWeaver does not emit a candidate list, mechanism label, action hint,
priority, or optimization opportunity ranking. Choose and justify the candidate directly from the
measured facts; prove specific data reuse, dependency, and correctness conditions in the emitted
slices.
If the brief reports a `VARIANTS` line, read only those named sibling slices. A promising sibling
may be in the measured file or another declared source file; it can be adapted as one coherent
candidate, including linked caller, producer, transfer, data, and kernel edits, so the search can
reach existing structural alternatives without browsing the whole benchmark. The compact
`DEPENDENCY FRONTIER` names target IDs and parent edges; use `source-targets.json` only to resolve
the connected slice needed by the proposed change.
If the brief reports pre-ROI transfer measurements, treat them as collateral timing facts. Use
them only when the declared objective includes whole-program or end-to-end time, while protecting
the primary ROI.

The controller may measure one candidate per step to keep the evidence compact, but it does not
choose or restrict that candidate. A candidate may contain several dependency-linked edits across
multiple files, including a cross-file representation and producer/consumer rewrite. Continue
from the accepted measured parent, or deliberately branch from the preserved best candidate after
a regression. Repeat or combine a structural family whenever the source evidence supports it.
Do not mix unrelated algorithms, flags, and layouts without a stated causal hypothesis. Preserve
numerical, communication, and synchronization semantics.

Consume only the compact result and its named source slices. Never browse the application, raw
trace, full counter table, profiler stdout, or large JSON broadly. Resolve one named dependency at
a time. Full streams and captures stay under `raw/`.

Accept only a correct, statistically significant absolute improvement under the same fingerprint
whose candidate profile shows the targeted cost fell and collateral cost did not erase the gain. An
unresolved gate is `inconclusive`. Record the investigation cost through the controller's final
workflow.
