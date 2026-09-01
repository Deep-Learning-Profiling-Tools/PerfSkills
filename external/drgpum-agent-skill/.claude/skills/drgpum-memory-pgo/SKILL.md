---
name: drgpum-memory-pgo
description: Find, quantify, and fix GPU memory inefficiencies in CUDA and PyTorch applications using DrGPUM memory_liveness profiles. Use when the user has a DrGPUM output directory, wants to reduce peak GPU memory, is hitting CUDA out-of-memory, or wants a larger batch on fixed hardware. Not for kernel timing, SM occupancy, or instruction-level tuning — DrGPUM records no timing counters; use Nsight Compute for those.
---

# DrGPUM Memory-PGO

Profile-guided optimization for **GPU memory**, not GPU time.

DrGPUM answers one question well: *which bytes are resident on the device
without earning their place there?* If a question is about how fast a kernel
runs, this is the wrong tool — say so and stop.

## When to use

- A DrGPUM `memory_liveness` output directory exists and needs interpreting
- Peak GPU memory is the constraint (OOM, or batch size capped by memory)
- A memory fix was applied and needs verifying

## When not to use

- Kernel latency, occupancy, or bandwidth questions → Nsight Compute
- No profile yet and no GPU access → nothing here can be inferred without data
- The goal is end-to-end wall-clock speedup → DrGPUM cannot measure time

## Reference material (load on demand)

Read these only when the step calls for them; they are large.

| File | Read it when |
|---|---|
| `references/output-format.md` | parsing raw files, or a format looks wrong |
| `references/detectors.md` | interpreting or ranking findings |
| `references/pytorch-allocator-segments.md` | **the workload is PyTorch** |

## Workflow

### Step 1 — Parse and rank. Never skip.

```bash
python scripts/analyze_profile.py <profile_dir> --json findings.json
```

Read `findings.json` before forming a hypothesis. Each finding already carries
`evidence`, `harm`, and `fix`.

### Step 2 — Pick exactly one finding

Highest severity that is **live at `memory_peak_kernel`**. Only allocations
resident at the peak can lower the peak.

One finding per iteration. Batching changes makes the re-profile
uninterpretable.

### Step 3 — Locate it in source

Findings carry an object id, not a line number. In order of reliability:

1. **`memory_liveness.csv.context`** — call path per context id.
   `Profile.call_stack_for(obj)` joins it automatically.
2. `torch_python_states.txt` — Python frames, if built with torch-monitor.
3. Match `size_bytes` against tensor shapes × dtype.
4. Correlate the alloc op_id against `kernel_list.txt` ordering.

State which you used. **If none identifies the site, report it unlocated and
stop.** Do not guess at a tensor.

### Step 4 — Apply the fix

See the fix column in `references/detectors.md`.

### Step 5 — Verify correctness before performance

Run the application's tests, or compare output tensors against the pre-change
run within tolerance. **A memory reduction that changes results is not an
optimization.** On failure, revert and record `rejected: breaks correctness`.

### Step 6 — Re-profile and compare

```bash
python scripts/compare_profiles.py <before_dir> <after_dir>
python scripts/show_ledger.py
```

Report the delta in `current_memory_peak`, measured identically both times.

## Rationalizations

Excuses for skipping the steps above, and why each is wrong. Every one of these
was actually made — or nearly made — while building this skill.

> **"The peak dropped 30%. I'll report it now and check correctness after."**

No. Freeing memory the program still needed produces a beautiful profile and
wrong answers. Correctness is step 5 *before* the number is reported, not after.
An unverified reduction is not a result.

> **"I can't locate the object, but it's 20 MB and the model has a 20 MB tensor. Close enough."**

This exact reasoning was wrong on this project. The 20 MB object was PyTorch's
`kLargeBuffer` allocator segment; the model's parameters totalled 2.51 MiB. The
size coincidence was meaningless. Unlocated means unlocated — say so.

> **"I'll fix three findings at once to save a profiling cycle."**

Then the re-profile attributes nothing. You will know the peak moved and not
which change moved it, and a regression in one fix will be masked by a gain in
another. Profiling cycles are cheap; ambiguous results are not.

> **"`hpcprof` is slow. I'll skip attribution and infer the source from the size."**

See rationalization two. If attribution is genuinely unavailable, bisect the
*workload* — profile progressively smaller programs until the allocation
appears. That is what actually identified the 20 MB object.

> **"It says NEVER_FREED, so it's a memory leak."**

In a PyTorch process, usually not. The caching allocator holds device blocks
until exit by design. Check whether the object grows across iterations before
calling anything a leak — profile at two iteration counts and compare.

> **"`optimal_memory_peak` is 0, so 100% of the peak is recoverable."**

Zero means the analysis did not run, not that zero bytes were needed. Rank by
per-object severity instead.

> **"The profile is small, but the findings will scale to the real workload."**

Memory findings do not extrapolate that way. Object *count* and *sizes* were
identical at 5 and 150 iterations here — but that is a measured fact about this
workload, not a general law. Measure it; do not assume it.

> **"The user just wants a number. All this verification is overhead."**

The verification is the deliverable. A memory reduction without a correctness
check and a before/after measurement is an assertion, and assertions are what
this project already had too many of.

## Red flags

Stop and re-read the reference material if you catch yourself:

- quoting a runtime, occupancy, or bandwidth figure from a DrGPUM run
- parsing `memory_liveness.csv` as comma-separated columns
- reporting a peak in exabytes (u64 underflow — see `references/output-format.md`)
- naming a tensor you did not locate through one of the step-3 methods
- comparing two profiles captured with different batch sizes or input data
- reporting an estimated speedup

## Termination clauses

Stop and report when any holds. Hitting one is a **successful outcome**.

1. Peak reduction **< 2%** on the last iteration — below run-to-run noise.
2. `current_memory_peak` within **10%** of `optimal_memory_peak`.
3. **Three consecutive iterations** with no accepted fix.
4. **Correctness failed twice** on the same finding — the finding is wrong here.
5. **No finding above 1% severity** remains.
6. The object cannot be located in source.

`Ledger.should_stop()` checks 1, 3, and 4 mechanically.

## Memory

**Ledger — append only, never edited.** One row per iteration: finding kind,
object id, action, peak before/after, correctness. This is the experimental
record; the writeup is built from it. Rejected attempts stay in it — "we tried X
and it broke correctness" is a result. See `assets/ledger-template.jsonl`.

**View — recompute, never cache.** Parsed profiles and rankings. Re-derive each
iteration; a profile from two fixes ago describes a program that no longer
exists.

**Policy — the accumulating lesson.** Which fix kinds worked on this codebase,
which allocation sites resisted, which findings were false positives and why.
Consult before step 2.

Do not re-read the whole conversation to answer "what did we already try" —
read the ledger.

## Reporting

Use `assets/finding-report-template.md`. Every finding answers, in order:

1. **What** — object id, size, and the op sequence proving it
2. **Why harmful** — idle byte-kernels, and what that costs concretely
3. **How fixed** — the source change, plus measured before/after peak

A finding missing any of the three is not ready to report.

## Reference

Mao Lin, Keren Zhou, Pengfei Su. *DrGPUM: Guiding Memory Optimization for
GPU-accelerated Applications.* ASPLOS '23.
https://doi.org/10.1145/3582016.3582044
