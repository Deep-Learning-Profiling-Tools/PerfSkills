# NVIDIA Nsight Compute mechanics

Load this only for an NCU run, a counter interpretation question, or a tool failure. Installed
help is authoritative because section names and metrics vary by GPU and NCU release.

## Verified local interface

The 2025.3.0.0 build tested on 2026-10-17 advertises `basic` (213 estimated metrics), `detailed`
(697), and `full` (7580). A bounded XSBench large-input run collected 2,374 numeric metrics for
one matching kernel in 28.979 seconds and exported raw/details/source views in 2.869 seconds.
That is a command-mechanics and cost observation for this run, not representative application
overhead. `full` contains ComputeWorkloadAnalysis, InstructionStats,
LaunchStats, MemoryWorkloadAnalysis, Occupancy, SchedulerStats, SourceCounters, SpeedOfLight,
WarpStateStats, roofline charts, and platform-specific sections. These are availability counts,
not capture results or overhead measurements.

The CLI accepts `--section <identifier>` and `--set <identifier>`. It does not advertise a
section named `all`. ProfileWeaver therefore defines `capture.py --section all` as an alias for
the installed all-sections set `--set full`, and records requested plus resolved modes in
`capture.json`.

## Required preflight

Run these once per environment and cache their output:

```bash
ncu --version
ncu --list-sets
ncu --list-sections
ncu --help
nvidia-smi
```

Also run a one-launch permission probe. If hardware counters are denied, report the limitation
and continue with timeline evidence; do not fabricate occupancy, stalls, or throughput.

## Bounded collection

Full replay can require thousands of metrics and many kernel replays. Require all of:

- a timing-ranked kernel name;
- an exact/escaped regex kernel filter;
- a small `--launch-count` (default one);
- a representative input and identical fingerprint;
- capture and export wall time recorded separately;
- raw output kept off-context.

The wrapper uses kernel replay, demangled kernel matching, and exports three views: raw metrics
(required), details/rules (best effort), and source/SASS (best effort). The report remains the
source of truth. A replay duration is measurement cost, not application runtime.

## Compact evidence dictionary

The reducer preserves each NCU metric name beside its value. Common fields include:

| Capsule field | Typical NCU metric family | What it supports |
|---|---|---|
| registers/thread | `launch__registers_per_thread` | register footprint; pressure needs an occupancy/resource link |
| achieved occupancy | `sm__warps_active...pct_of_peak...` | resident active-warps share |
| occupancy resource limits | `launch__occupancy_limit_*` | which launch resource constrains theoretical residency |
| active/eligible/issued warps | `smsp__warps_{active,eligible,issued}.avg` | scheduler supply and issue opportunity |
| warp stalls | `smsp__warp_issue_stalled_*_per_warp_active.pct` | leading reasons active warps could not issue |
| SM/DRAM/L1/L2 throughput | `*__throughput...pct_of_peak...` | proximity to measured sustained subsystem ceilings |
| cache hit rates | `l1tex__t_sector_hit_rate*`, `lts__t_sector_hit_rate*` | cache outcome; locality cause still needs source/access evidence |
| sectors/request | `*average*sectors_per_request*` | transaction/coalescing symptom where available |
| launch shape | `launch__{grid,block}_size`, shared memory, waves/SM | geometry and residency inputs |
| instructions/branch behavior | `smsp__inst_executed*`, branch-efficiency family | work and control-flow evidence where collected |
| Roofline chain | NCU `SpeedOfLight_RooflineChart` work/traffic rates and peaks | FP32/FP64 resource bound, kernel opportunity ceiling, then Amdahl ROI ceiling with timing data |

Metric availability varies. Missing data remains null/absent. Never rename an unrecognized raw
metric into a familiar concept merely because its label sounds similar.

The Roofline reducer follows the equations in the installed NVIDIA section file rather than a
datasheet peak. It selects the precision with the largest measured work rate. Its kernel and ROI
values are opportunity ceilings, not predicted achieved speedups; integer-only kernels or missing
work/traffic counters remain explicitly unavailable.

`kernel-diagnostics.json` is the default decision vector. Its `drilldowns` map points to bounded
semantic files for `launch`, `occupancy`, `scheduler`, `stalls`, `instructions`, `control_flow`,
`memory`, `throughput`, and `roofline`. These retain relevant raw metric names and report matched,
selected, and omitted counts. Read at most one group for a concrete unresolved question. The raw
CSV is lossless and remains evidence-plane data rather than agent context.

## Interpretation rules

- Rank kernels using uninstrumented or Nsight Systems time, never NCU replay wall time.
- A high stall share says why warps did not issue during sampled active cycles; it does not by
  itself say that eliminating that stall will improve end-to-end runtime.
- Low achieved occupancy can be sufficient for latency hiding. Require low eligible warps or a
  measured throughput/latency symptom before optimizing occupancy.
- Registers per thread alone are not register pressure. Connect them to the occupancy limit,
  spills/local-memory traffic, or an A/B compiler-resource change.
- High DRAM throughput can support bandwidth saturation; low bandwidth plus long-scoreboard
  stalls can support latency. Evaluate both with cache and access-pattern evidence.
- Divergence needs branch/thread-efficiency evidence. Branch count or a source `if` is not enough.
- Full replay perturbs execution and may serialize launches. Use it for ratios/counters inside a
  kernel, not whole-program timing or overlap.
- Compare baseline and winner only with the same GPU, clocks/power state, input, kernel scope,
  set/sections, replay mode, and launch filter.

## Common failures

- `ERR_NVGPUCTRPERM`: hardware counter access is blocked. Record it as an unresolved gate.
- No kernels matched: inspect the timing profiler's exact demangled name and escaped regex; do
  not broaden to every kernel.
- Multiple kernels matched: narrow the expression or set a deterministic skip/count.
- Metric unavailable: keep null and use another supported metric or tool.
- Excessive runtime: preserve the full request for the required measurement, but reduce the
  representative input only after proving the same hotspot and bottleneck are retained.
