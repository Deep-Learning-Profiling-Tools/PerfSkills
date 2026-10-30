---
name: pw-nsight-compute
description: Diagnose one measured NVIDIA GPU kernel with Nsight Compute replay counters, then return bounded evidence for warp stalls, scheduler eligibility, registers, occupancy, cache/memory behavior, launch shape, and throughput. Use after a timing profiler identifies an expensive kernel or when an NCU report already exists. Not for application timelines, CPU gaps, transfers, launch overhead, MPI waits, or whole-program timing.
---

# Nsight Compute child

Own kernel-internal `ncu` evidence. Start from a kernel named by `pw-nsight-systems` or another
timing child; never replay every kernel. Reuse cached preflight for `ncu --version`, available
sets/sections, device access, and counter permission. Preserve the exact executable, input,
environment, launcher, device mapping, oracle, and baseline fingerprint.

Collect one matching launch through the ProfileWeaver experiment controller. Its backend resolves
`section=all` to the installed NCU all-sections mode, records the exact resolved command, captures
the report, exports raw/details/source pages, reduces them, validates the artifacts, and caches the
result. The agent must not invoke the capture or reducer scripts directly during a ProfileWeaver
journey.

The capture emits one terminal record containing status, NCU capture time, export time, and the
metadata path. Target output, profiler output, the `.ncu-rep`, all metrics, rules, and source
views stay under `raw/`. The reducer emits at most two kernels, three leading warp stalls, three
measured signals, and twelve provenance-bearing metrics per kernel in the default capsule. It
also writes named bounded drill-downs for launch, occupancy, scheduler, all available stall
families, instruction mix, control flow, memory, throughput, and roofline. Load one group only
when it can distinguish the next optimization; unknown fields stay absent or null.

Use the compact diagnostic capsule with the timing profile and named source slice. Registers per
thread describe footprint; call it register pressure only when occupancy/resource evidence
supports that conclusion. Likewise, stall shares, cache rates, occupancy, and throughput are
joint evidence, not isolated optimization goals. Read `references/tool-notes.md` for metric
semantics, replay caveats, permission failures, or a version mismatch.

Under the root ProfileWeaver workflow, the agent invokes only `shared/scripts/experiment.py`;
the capture and reducer are backend details. Use a baseline timeline
once to establish absolute cost. Rank candidates with CUDA-event/ROI timing plus a whole-process
collateral check. Every correct changed kernel receives filtered NCU evidence and parent- plus
baseline-relative deltas; an unchanged kernel explicitly reuses its fingerprinted evidence.

Baseline and accepted-winner captures use the full set. Candidate capture follows a hierarchy:
launch/resource and scheduler/warp evidence are the core; memory, compute/instruction, source, or
control-flow sections are added according to the current diagnosis. The verified
`SpeedOfLight_RooflineChart` section accompanies automatic candidate tiers so the backend can
update the Roofline → kernel → Amdahl chain each iteration. Only section names verified by cached
preflight may be used. If no verified focused tier exists, collect the full set rather than guess
a flag. A new diagnosis advances the next candidate's tier.

NVTX may bound an existing stable application phase and a kernel regex selects the measured
kernel inside it. NVTX identifies scope; a CUDA event or application ROI timer supplies candidate
time. NCU replay duration is recorded separately as profiling cost. Accept a rewrite only when
absolute runtime improves under the same fingerprint and the relevant counter cost also falls
without a larger collateral regression.
