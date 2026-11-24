---
name: pw-cupti
description: Collect CUDA activity timing or current CUPTI PC-sampled instruction stalls for a bounded GPU evidence question. Modes are separate; neither provides cache traffic, achieved occupancy, or GPA causal blame.
---

# CUPTI evidence

The adaptive controller uses `shared/scripts/cupti_activity.py` and the locally compiled
`shared/native/cupti_activity.cpp`. Continue through `experiment.py`; no application source
instrumentation or changed build flags are required.

This provider records concurrent kernel durations/counts and memcpy bytes/durations. With a
declared ROI it enables CUPTI NVTX markers through the installed libcupti NVTX injection,
matches the first complete named range, clips intervals, subtracts named exclusions, and
computes the activity interval union. Outside-region setup remains collateral. No range is
inferred from an application timer or kernel name. The ROI path currently requires one process;
missing/incomplete markers, truncated phase records, dropped records or collection errors
prevent complete ROI evidence and baseline collection falls back to a capable context tool.
Existing conflicting injection is preserved. Without a declared ROI this remains whole-process
activity, whose summed durations are not busy union or application ROI.
It does not serialize kernels or collect replay counters. Process IDs separate MPI/child outputs.
Profiling execution stays separate from unprofiled ranking/confirmation.

Use for hotspot movement, kernel/copy counts and observed NVTX-region attribution. Use NSYS for
host gaps, synchronization, detailed context and missing ranges; use NCU for hardware counters.
Activity timestamps do not establish synchronization semantics, cache behavior or stalls.
Probe/build failures are explicit. Phase interval retention is bounded at one million activity
intervals and 100,000 markers; exceeding either bound cannot produce trusted ROI evidence.

Reference: [CUPTI Activity API](https://docs.nvidia.com/cupti/api/group__CUPTI__ACTIVITY__API.html).

For `gpu-pc` or `instruction-stalls`, the adaptive router can instead select
`shared/scripts/cupti_pc.py`. It builds the current installed SDK's PC collector/decoder in a
content-keyed cache and probes the actual device/API. It never changes the installed SDK or
application. Collection covers the process; reduction filters the already measured target.
PID/context suffixes isolate MPI outputs. Zero samples, dropped samples, application failure,
or changed binary identity prevent complete evidence.

Return literal stall metrics, counts and instruction offsets. `*_not_issued` is a subset of its
parent sampling population and is never added to it. PC sampling does not provide kernel time,
causal blame, cache transactions, achieved occupancy, or guaranteed source lines. Never call it
GPA or rank candidates with instrumented timing. Activity and PC costs are learned separately.

Reference: [current CUPTI PC Sampling API](https://docs.nvidia.com/cupti/api/group__CUPTI__PCSAMPLING__API.html).
