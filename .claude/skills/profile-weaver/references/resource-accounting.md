# Resource accounting: follow-on design

Keep the acquisition-policy work first. This document specifies the next layer; it does not
claim that energy, utilization, or GPU-hour accounting has already been implemented.

Record one ledger row per build, correctness check, paired execution, profiler capture, export,
reduction, and cache hit. Existing campaign and acquisition wall timers provide initial facts.
Include timestamps, phase, device/rank assignment, executable/input fingerprints, tool, and status.
Distinguish failed attempts from useful evidence and accepted optimizations.

| Quantity | Measurement and attribution |
|---|---|
| Allocated GPU-hours | Wall-clock reservation duration × distinct allocated GPUs / 3600. Read scheduler allocation; never multiply a shared GPU once per MPI rank. Unknown allocation stays null. |
| GPU activity hours | Per-device union of timestamped GPU activity intervals / 3600. Summed overlapping kernel durations are not this quantity. |
| GPU utilization | Optional low-rate NVML/DCGM sampling with measured observer cost; short workloads may be below its temporal resolution. Report sample coverage. |
| Energy | Prefer a supported cumulative device-energy counter; otherwise explicitly report power integration and sampling uncertainty. Shared-device energy is not attributable to one campaign without isolation. |
| CPU time/core-hours | Process-tree CPU usage for builds, profilers, reducers, and application separately; scheduler-reserved core-hours are a distinct cost. |
| Memory and storage | Peak GPU allocation and host RSS where measurable; distinguish allocated, resident, and live bytes. Record trace/artifact bytes and retention. |
| Tokens | Loaded definitions and supplied session accounting separately. Missing session totals remain null. |

Report cost per useful evidence acquisition, cost per verified improvement, time to first verified
gain, and end-to-end campaign cost. Compare retained performance at equal time/GPU-hour/token
budgets, including rejected candidates and failed profilers. Savings in an acquisition do not
automatically imply equal savings in total campaign time.

In new adaptive campaigns, `inspect --section costs` includes controller command wall seconds,
process and reaped-child CPU seconds, allocated GPU-hours over those command intervals, energy
and externally supplied session tokens. Capture-level rows calibrate selection and are excluded
from command totals. Gaps between controller commands and client thinking time lie outside this
meter. A missing measurement in any command leaves the corresponding aggregate null.

Set `profiling.skill_runtime.resources.allocated_gpus` when the allocation is known. The default
can use the node-local GPU count in a Slurm job. For an exclusive physical allocation, supply
`exclusive_gpu_uuids` to read cumulative NVML energy, or `exclusive_energy_j_path` for an
operator-provided cumulative joule meter. A reset or unavailable meter yields null.
Record cumulative campaign-specific client tokens with `experiment.py PLAN usage --session-tokens N`.
Two readings around an isolated evidence command can calibrate its provider's token cost.
Delivered packet tokens remain a separate quantity.

Use measured costs to update provider priors by capability, device, input regime, and phase.
Preserve a final-confirmation reserve. Never trade away correctness, replay-loss checks, or final
acceptance gates merely to improve an efficiency score. Uncertain signals should purchase a
targeted measurement, not generate an invented speedup or resource estimate.
