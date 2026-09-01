---
name: benchmark-validator
description: Compare before-and-after DrGPUM profiles to verify a memory optimization actually worked, and decide whether to iterate or stop. Use after applying a fix and re-running the profiler. Not for measuring runtime speedup -- DrGPUM has no timing data.
---

# Stage 3: Benchmark Validator

**Input:** two DrGPUM output directories captured with **identical workload
configuration** -- same batch size, same input data, same model. A comparison
across different configurations measures nothing.

## Correctness first

**Verify the program still produces correct results before looking at memory.**
Run the application's own tests, or diff output tensors against the pre-change
run within tolerance.

A memory reduction that changes results is not an optimization. If correctness
fails, revert and record the finding as `rejected: breaks correctness` -- the
finding was wrong about that object's lifetime, and that is itself a result
worth writing down.

## Compare

```bash
python -m agent.ml_pgo_agent --baseline before/ --optimized after/
```

Reports:

- `current_memory_peak` before vs after, in bytes and percent
- which findings were resolved
- which findings are new (a fix can introduce one)

The peak delta, measured the same way on both runs, is the result. Report it as
bytes, not as a ratio alone.

## Termination clauses

Stop and report when any of these holds. Hitting one is a successful outcome.

1. Peak reduction **< 2%** on the last iteration -- below run-to-run noise.
2. `current_memory_peak` within **10%** of `optimal_memory_peak` -- what remains
   is allocator behaviour, not program structure.
3. **Three consecutive iterations** with no accepted fix.
4. **Correctness failed twice** on the same finding.
5. **No finding above 1% severity** remains.
6. The object cannot be located in source.

## Record every iteration

Append one line to the ledger, never edit a previous one:

```
iteration, finding kind, object id, action taken, peak before, peak after, correctness pass/fail
```

This is the experimental record. Rejected fixes stay in it -- "we tried X and
it broke correctness" is a finding, and the writeup is built from this file.

## Do not

- Report a memory reduction without stating that correctness was checked.
- Claim a speedup. DrGPUM measures bytes, not time; an end-to-end timing claim
  needs a separate timed benchmark run outside the profiler.
- Compare runs with different workload configuration.

## Rationalizations

> **"Peak dropped 30%. Ship it."**

Not until correctness passes. A fix that frees memory the program still needed
shows an excellent profile and produces wrong results. Correctness is the gate,
not a follow-up.

> **"Different batch size, but it's the same code — close enough to compare."**

Then the comparison measures the batch size. Peak memory is dominated by
workload configuration; both runs must be identical in batch size, input data,
and iteration count or the delta means nothing.

> **"The fix broke correctness. I'll tweak it and try again right away."**

Once is a bug in the fix. Twice on the same finding means the *finding* is wrong
about that object's lifetime — termination clause 4. Record why and move to the
next finding.

> **"Only 1.5% improvement, but it's still an improvement."**

It is below the run-to-run noise floor, so you cannot distinguish it from
nothing. Clause 1 says stop, and stopping is a successful outcome — not a
failure to be optimized around.

> **"Three iterations with nothing accepted. Let me try harder."**

Clause 3 exists precisely for that impulse. Report what was tried and what the
remaining gap is. A clean negative result is publishable; a fourth flailing
iteration is not.

> **"The rejected attempts are noise — I'll report only what worked."**

The rejected attempts are the experimental record. "We freed X at last use and
the loss diverged" tells the next reader something real about the program, and
stops the next iteration repeating it. The ledger is append-only for this reason.
