---
name: pw-nsight-compute
description: Diagnose one measured NVIDIA GPU kernel with Nsight Compute replay counters, then return bounded evidence for warp stalls, scheduler eligibility, registers, occupancy, cache/memory behavior, launch shape, and throughput. Use after a timing profiler identifies an expensive kernel or when an NCU report already exists. Not for application timelines, CPU gaps, transfers, launch overhead, MPI waits, or whole-program timing.
---

# Nsight Compute child

Own kernel-internal `ncu` evidence. Start from a kernel named by `pw-nsight-systems` or another
timing child; never replay every kernel. Reuse cached preflight for `ncu --version`, available
sets/sections, device access, and counter permission. Preserve the exact executable, input,
environment, launcher, device mapping, oracle, and baseline fingerprint.

Collect one matching launch. ProfileWeaver's `--section all` is a portable intent: the capture
wrapper validates it as the installed NCU all-sections set and records the resolved command. On
the tested 2025.3 build this becomes `--set full`; it never sends the invalid literal
`ncu --section all`.

```bash
python3 .claude/skills/pw-nsight-compute/scripts/capture.py \
  --output-dir runs/<id>/raw/ncu-baseline \
  --kernel-name '<escaped-kernel-regex>' --launch-count 1 --section all \
  --tool-version '<cached-preflight-version>' -- <exact workload command>

python3 .claude/skills/pw-nsight-compute/scripts/reduce_csv.py \
  --csv runs/<id>/raw/ncu-baseline/metrics.csv \
  --capture-metadata runs/<id>/raw/ncu-baseline/capture.json \
  --timeline-profile runs/<id>/profile.baseline.json \
  --run-id <id> --phase baseline \
  --output runs/<id>/profile.ncu.baseline.json \
  --diagnostics-output runs/<id>/kernel-diagnostics.json
```

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

For iteration, keep Nsight Systems as the per-candidate timing child. Cache this expensive full
NCU baseline by fingerprint. Recollect NCU only when a correct candidate changes the diagnosed
kernel mechanism, or for a fresh accepted-winner reprofile. Accept a rewrite only when absolute
runtime improves under the same fingerprint and the relevant NCU cost also falls without a
larger collateral regression.
