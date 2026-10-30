# Hierarchical diagnosis and performance models

ProfileWeaver uses the idea behind GPU Performance Advisor's instruction blamer and optimizer
catalog: attribute measured stalls to the narrowest responsible scope, match evidence patterns to
candidate mechanisms, then measure the rewrite. A model ranks experiments; it never replaces the
correctness oracle or measured speedup.

The hierarchy is application → kernel → function/data → instruction. Stop at the first level that
supports a decision. Escalate only the selected hotspot.

| Evidence | Model or bound | What it can prioritize | What it cannot claim |
|---|---|---|---|
| NCU SpeedOfLight work/traffic rates and peaks | Roofline kernel ceiling | compute vs. DRAM bound and attainable kernel headroom | that a particular rewrite reaches the roof |
| Roofline kernel ceiling + timing-profiler hotspot fraction | Amdahl application ceiling | whether a kernel opportunity can change the application ROI | achieved end-to-end speedup |
| hotspot fraction | Amdahl removal ceiling | whether the target can matter | achieved speedup |
| useful bytes per 32-byte sector | transaction-efficiency bound | coalescing, packing, search/index and layout changes | latency or wall-time gain |
| measured DRAM bytes and kernel duration | observed traffic rate | traffic removal vs. arithmetic work | peak bandwidth without device facts |
| active and eligible warps, issue activity, stalls | latency-hiding ratio | dependency chains, parallelism, locality, divergence | a unique root cause alone |
| registers, spills, occupancy limiters | resource-feasibility model | live-range reduction, launch-shape experiments | that lower occupancy is bad by itself |
| H2D/D2H bytes and phase time | transfer lower bound | residency, larger data regions, batching | overlap benefit without a timeline |
| static search depth/work/footprint | algorithmic work model | indexing, binning, precomputation, layout | runtime until measured |
| accumulated candidate outcomes | empirical residual/prior | order equally supported mechanisms | override current evidence or gates |

Use analytical outputs as a score or upper/lower bound with their inputs attached. Do not label a
model estimate as a profiler measurement. A self-evolving policy may learn profiler cost and
mechanism success after repeated comparable runs, but it may only break capability/cost ties and
order experiments. It cannot edit skills, thresholds, source, or correctness rules.

## Roofline → Amdahl chain

For FP32 or FP64 kernels, the NCU reducer follows the equations in NVIDIA's installed
`SpeedOfLight_RooflineChart` section. It selects the precision with the largest measured work rate
and computes:

```text
arithmetic intensity = achieved FLOP/s / achieved DRAM B/s
ridge point          = peak FLOP/s / peak DRAM B/s
roofline performance = min(peak FLOP/s, peak DRAM B/s × arithmetic intensity)
kernel ceiling       = roofline performance / achieved FLOP/s
ROI ceiling          = 1 / ((1 - hotspot fraction) + hotspot fraction / kernel ceiling)
```

The profiler supplies achieved and peak rates for the active device; no GPU model or datasheet
peak is guessed. The hotspot fraction comes from the timing profile and is never reconstructed
from replay time. Missing counters, zero floating-point work, or a missing timing fraction leave
the corresponding model incomplete. The value is an opportunity ceiling for ranking candidates;
benchmark and reprofile measurements replace it after an edit.

```text
GPA-style symptom/blame family
  → Roofline resource bound
  → mechanism-specific candidate
  → kernel opportunity ceiling
  → Amdahl application ceiling
  → measured candidate delta
```

Primary background: *GPA: A GPU Performance Advisor Based on Instruction Sampling*, CGO 2021,
DOI 10.1109/CGO51591.2021.9370339.
