# Finding: <KIND> on object <id>

Template for reporting one inefficiency. Every section must be filled from
profiler data; leave a section blank rather than guessing at it.

## What

- Object id, size in bytes, and the op sequence proving the claim
- Where in the trace: alloc op_id, first/last use op_id, free op_id
- Live at `memory_peak_kernel`? (if no, say so — it cannot reduce peak)

## Why it is harmful

- Idle byte-kernels, and severity as a share of the run's budget
- What that costs concretely: peak memory, achievable batch size, OOM risk
- Not: an estimated speedup. DrGPUM cannot measure time.

## Where in the source

- Method used: `memory_liveness.csv.context` call path / torch_python_states /
  size match / kernel-order correlation
- The call stack or file:line
- If unlocated: say "unlocated" and stop. Do not guess.

## The fix

- The exact code change
- Why it is semantics-preserving
- How correctness was verified (test suite / output comparison)

## Result

- `current_memory_peak` before and after, in bytes
- Correctness: pass / fail
- Accepted or reverted, and why
- Ledger entry appended: yes/no
