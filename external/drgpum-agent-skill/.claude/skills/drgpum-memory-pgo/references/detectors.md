# Detectors and the harm metric

Implementation: `agent/drgpum/detectors.py`. Run via
`scripts/analyze_profile.py`.

## The six detectors

| Kind | Condition | Fix |
|---|---|---|
| `NEVER_USED` | ALLOC with no ACCESS, SET, COPYT or COPYF ever | delete the allocation |
| `LATE_FIRST_USE` | long gap between ALLOC and first use | sink the allocation to first use |
| `DELAYED_FREE` | long gap between last use and FREE | free at last use; narrow the scope |
| `NEVER_FREED` | ALLOC with no FREE | check the allocator first (see below) |
| `REDUNDANT_H2D_COPY` | COPYT overwritten by COPYT with no read between | hoist or drop the transfer |
| `PEAK_MEMORY_GAP` | `current_memory_peak > optimal_memory_peak` | the budget the others draw down |

Defaults: objects under 64 KB and idle spans under 8 kernel launches are
ignored. Lower with `--min-bytes` / `--min-idle-kernels` if a run reports
nothing.

**One object can carry several kinds at once.** That is normal — an object
allocated early and freed late is both `LATE_FIRST_USE` and `DELAYED_FREE`.
Rank them; do not deduplicate.

## Measuring harm: idle byte-kernels

DrGPUM emits no timestamps, so idle spans are counted in **kernel launches** —
the only countable unit of progress in the trace. Harm is:

```
idle_byte_kernels = size_bytes × kernels_launched_while_untouched
```

the discrete analogue of the memory×time integral that DrGPUM's own
`optimal_memory_peak` is computed against.

`severity` normalises that against the run's total byte-kernel budget
(`peak_bytes × total_kernels`), so it ranks findings **within one run**. It is
not comparable across runs with different kernel counts.

## The rule that decides what to fix first

**Only allocations live at `memory_peak_kernel` can reduce peak memory.**

A large `DELAYED_FREE` whose whole lifetime falls after the high-water mark is
genuine waste and will not move the headline number. Check liveness at the peak
before ranking a finding first.

## PyTorch caveat

PyTorch's caching allocator holds device blocks until process exit **by
design**, so most `NEVER_FREED` findings in a torch run are the allocator
working correctly, not leaks. Treat one as actionable only if the object grows
across iterations or is large and never reused.

A cheap check for a genuine leak: profile the workload at two iteration counts.
If object ids, sizes, and peak are identical, nothing accumulates. Measured on
`train.py` at 5 vs 150 iterations — identical, so no leak.

Deeper caveat in `pytorch-allocator-segments.md`: without `torch-monitor`,
every object DrGPUM reports for a PyTorch process is an allocator *segment*
rather than a tensor, and the top findings describe the allocator instead of
the application.
