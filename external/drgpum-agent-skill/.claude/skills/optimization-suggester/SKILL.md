---
name: optimization-suggester
description: Turn ranked DrGPUM memory findings into concrete source changes for CUDA or PyTorch code. Use after drg-profile-analyzer has produced findings and the user wants to know what to actually edit. Not for kernel-level or algorithmic speed tuning.
---

# Stage 2: Optimization Suggester

**Input:** `findings.json` from `drg-profile-analyzer`, plus the application
source.

## Pick one finding

Highest `severity` that is **live at `memory_peak_kernel`**. Fix exactly one
per iteration -- batching changes makes the re-profile uninterpretable.

## Locate it in source

Findings identify a DrGPUM object id, not a line of code. Bridge the gap by:

1. Matching `size_bytes` against tensor shapes x dtype in the model.
2. Reading `torch_python_states.txt` / `torch_libunwind_backtrace.txt` if the
   build has `REDSHOW_TORCH_SUBMEMORY_ANALYSIS` enabled -- these carry Python
   frames.
3. Correlating the alloc `op_id` against `kernel_list.txt` ordering to find
   which phase of the program allocated it.

**State which method you used.** If none identifies the site, report the
finding as unlocated and stop. Do not guess at a tensor.

## Fix table

| Finding | Change |
|---|---|
| `NEVER_USED` | Delete the allocation. If it is a "just in case" scratch buffer, confirm no path uses it; if a rare branch needs it, allocate inside that branch. |
| `LATE_FIRST_USE` | Sink the allocation to first use. Construct the tensor inside the loop or branch that consumes it instead of hoisting to setup. |
| `DELAYED_FREE` | `del` at last use, and drop stale references held by a list, closure, or `self.` attribute. Narrowing the variable's scope is more robust. |
| `NEVER_FREED` | Explicit free. Check for tensors accumulating across iterations -- appending a tensor that still carries its autograd graph is the usual cause; store `.detach()` or `.item()`. |
| `REDUNDANT_H2D_COPY` | Hoist loop-invariant transfers out of the loop, or delete the dead one. If data really changes per iteration, use pinned memory + `.to(device, non_blocking=True)`. |

## Every suggestion must state

1. The exact code change.
2. Why it is semantics-preserving.
3. How to verify correctness after applying it.
4. Expected effect on `current_memory_peak`, in bytes, from the finding's own
   `size_bytes` -- not an estimate you invented.

## Do not

- Suggest a fix whose benefit you cannot tie to a specific object id.
- Predict a runtime speedup. DrGPUM cannot measure one.
- Apply more than one fix before re-profiling.

Next: `benchmark-validator`.

## Rationalizations

> **"The object is 20 MB and the model has a 20 MB tensor. That's the one."**

This was wrong here. The 20 MB object was PyTorch's `kLargeBuffer` allocator
segment; the model's parameters totalled 2.51 MiB. Size coincidence is not
attribution — use one of the step-3 methods or report the finding unlocated.

> **"I couldn't locate it, but I'll suggest a fix anyway and let the user find the line."**

A fix aimed at an unidentified allocation is a guess with a confident tone. The
user cannot evaluate it, and applying it risks changing behaviour nobody
predicted. "Unlocated" is a legitimate, useful output.

> **"This is the biggest finding, so it goes first."**

Only allocations live at `memory_peak_kernel` can lower the peak. A 512 MB
`DELAYED_FREE` that lives entirely after the high-water mark is real waste and
will move the headline number by zero. Check liveness at the peak before ranking.

> **"I'll suggest all five fixes so the user can pick."**

They will apply several, re-profile once, and learn nothing about which one
worked. Name the one to do first and why.

> **"Freeing this saves 20 MB, which should speed things up too."**

DrGPUM measures bytes, not time. Any wall-clock claim needs a separate timed
benchmark outside the profiler. Do not convert memory savings into a predicted
speedup.

> **"The allocation looks unnecessary, so removing it is safe."**

"No ACCESS recorded" can mean the profiler cannot see the accesses — which is
exactly the case for PyTorch allocator segments without `torch-monitor`. Confirm
the tool can observe what you think it is observing before calling an allocation
dead.
