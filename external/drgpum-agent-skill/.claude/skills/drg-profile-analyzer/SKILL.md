---
name: drg-profile-analyzer
description: Parse and rank memory inefficiencies in a DrGPUM memory_liveness profile directory. Use when the user has run `gvprof -e memory_liveness` and needs to know what is wrong and how much it costs. Not for kernel timing or occupancy analysis -- DrGPUM measures memory, not time.
---

# Stage 1: DrGPUM Profile Analyzer

**Input:** the output *directory* from `gvprof -v -e memory_liveness ./app`.
Not a CSV. See `skills/drgpum-memory-pgo/SKILL.md` for the file formats.

## Run it

```bash
python -m agent.drgpum <profile_dir> --json findings.json
```

Detection is deterministic and runs locally. Read `findings.json` before
forming any hypothesis.

## What it reports

Whole-run:

- `current_memory_peak` vs `optimal_memory_peak`, and the gap between them
- `memory_peak_kernel` -- which kernel launch the high-water mark occurred at
- object count and kernel-launch count

Per object:

| Kind | Condition |
|---|---|
| `NEVER_USED` | ALLOC with no ACCESS, SET, COPYT, or COPYF ever |
| `LATE_FIRST_USE` | long gap between ALLOC and first use |
| `DELAYED_FREE` | long gap between last use and FREE |
| `NEVER_FREED` | ALLOC with no matching FREE |
| `REDUNDANT_H2D_COPY` | COPYT overwritten by COPYT with no read between |

An object can carry several kinds at once -- that is normal, not a bug. Rank by
`severity`, do not deduplicate.

## Ranking

`severity` is idle byte-kernels as a share of (peak bytes x kernel launches).
Because DrGPUM emits no timestamps, kernel-launch count is the clock.

Thresholds default to 64 KB and 8 kernel launches; lower them with
`--min-bytes` / `--min-idle-kernels` if a run reports nothing.

## Golden rule

**Only allocations live at `memory_peak_kernel` can reduce peak memory.** A
large `DELAYED_FREE` that lives entirely after the high-water mark is real
waste but will not move the headline number. Check this before recommending it.

## Do not

- Report kernel runtime, SM utilization, or memory bandwidth. DrGPUM records
  none of them.
- Parse `memory_liveness.csv` as columnar data -- it is a text op-trace.
- Compare profiles captured with different batch sizes or input data.

Next: `optimization-suggester`.

## Rationalizations

> **"The file is called `memory_liveness.csv`, so I'll parse it as CSV."**

It is a text op-trace: `op_id: 214, ALLOC 1000`. Reading that filename as a
promise, then inventing plausible column names for a GPU profiler, is precisely
how this project acquired a fabricated
`kernel_name,runtime_ms,sm_utilization_%,memory_bandwidth_%` schema that no tool
ever emitted. Check the bytes, not the extension.

> **"`optimal_memory_peak` is 0, so the entire peak is recoverable."**

Zero means the optimal-peak analysis did not run — it appears with
`memory_peak_kernel: 0`. Reporting "100% recoverable" from it sends the whole
optimization loop chasing a number that was never measured.

> **"The peak is 1.8×10¹⁹ bytes. That's what the profiler said."**

That is 16 exabytes on a 24 GB card. `output_ctx_node()` subtracts 512 from an
unsigned 64-bit peak, so any run under 512 B wraps. Sanity-check magnitudes
against the physical device before reporting them.

> **"Nine `NEVER_FREED` findings — this program leaks badly."**

If it is a PyTorch process, the caching allocator holds blocks until exit by
design. Profile at two iteration counts first: if object ids, sizes, and peak
are identical, nothing accumulates and nothing is leaking.

> **"I can see the shape of the problem already; I'll write the diagnosis and check the numbers after."**

Profile data is facts and everything else is a guess. The ranking exists so the
diagnosis follows the data rather than the other way round.

> **"This object appears under three different finding kinds. I'll deduplicate."**

Don't. An object allocated early and freed late genuinely is both a
`LATE_FIRST_USE` and a `DELAYED_FREE`; the overlap is information about its
lifetime. Rank, don't merge.
