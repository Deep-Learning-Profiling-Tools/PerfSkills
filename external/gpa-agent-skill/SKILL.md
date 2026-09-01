---
name: gpa-agent-skill
description: Full profile-guided-optimization loop for a GPA-profiled CUDA benchmark - analyze gpa.advice, locate the responsible source, apply one targeted optimization, verify correctness, and benchmark kernel and end-to-end speedup.
---

# gpa-agent-skill — GPA profile-guided optimization loop

**This is the implementation.** It is invoked through the `pw-gpa` child skill, which owns the
ProfileWeaver-facing contract (setup, gate mapping, adjudication). Read `pw-gpa`'s SKILL.md
first if you arrived here directly.

Paths below are written relative to the **ProfileWeaver root** — run the commands from there.
Every script needs `$PW_GPA_ROOT` set; see `pw-gpa` for setup.

You guide a full profile-guided-optimization loop for a GPA-profiled CUDA benchmark:
understand the profile, locate and analyze the responsible code, suggest and apply one fix,
verify correctness, and measure real local (kernel) and end-to-end speedup. Do not skip steps
and do not invent numbers — every claim must trace back to something you actually read or a
number a script actually printed.

The benchmark name (e.g. `bfs`, `huffman`) and its config live in
`external/gpa-agent-skill/assets/benchmarks.json`. If the user hasn't named a benchmark, ask which one.

## Workflow

### 0. Generate a fresh GPU profile

Always regenerate `gpa.advice` at the start of every invocation — the pipeline builds current
source first, so if a previous run's optimization was KEPT, this reflects that change rather than
acting on stale advice from before it:

```
python3 external/gpa-agent-skill/scripts/run_gpa_profile.py <benchmark>
```

This builds the benchmark, then runs `hpcrun -> hpcstruct -> hpcprof` and writes
`<benchmark-dir>/gpa-database/gpa.advice`. It only clears the pipeline's own intermediates
(`gpa-measurements`, `<binary>.hpcstruct`) — it never touches `pgo-baseline.json`,
`pgo-ledger.md`, or the `pgo-baseline-output-*` files that live in the same `gpa-database/`
directory. Do not run the old manual `hpcrun`/`hpcstruct`/`hpcprof` command sequence by hand —
this script replaces it (and fixes a real hazard the manual version had: a bare `rm -rf
gpa-database` would have destroyed the ledger and baseline alongside the pipeline's own output).

### 1. Capture a baseline (only if one doesn't already exist)

Check for `<benchmark-dir>/gpa-database/pgo-baseline.json`. If missing, run this *before* touching
any source — it is the reference every later comparison is measured against:

```
python3 external/gpa-agent-skill/scripts/pgo_bench.py baseline <benchmark>
```

### 2. Understand the profile

```
python3 external/gpa-agent-skill/scripts/get_profile_summary.py <benchmark> --top 5
```

This is a cached wrapper around `parse_advice.py`, keyed by `gpa.advice`'s content hash — on an
unchanged profile it returns the same JSON without re-parsing 1500-4000 lines of raw advice text,
and it persists the result to `<benchmark-dir>/gpa-database/profile-summary.json`. The cache is
purely an accelerant: if the script is ever unavailable, run `parse_advice.py
<benchmark-dir>/gpa-database/gpa.advice --top 5` directly instead — same output, just uncached.

If the output format needs interpreting (optimizer meanings, how `Hot BLAME` locations map to
source lines, the duplicate-kernel-block quirk), read
[references/gpa-advice-format.md](references/gpa-advice-format.md) — don't re-derive it from raw
`gpa.advice` text.

### 3. Check the ledger before proposing anything

```
python3 external/gpa-agent-skill/scripts/build_ledger_index.py <benchmark>
```

Then `Read` the resulting `<benchmark-dir>/gpa-database/pgo-ledger-index.json` — an O(1)
kernel -> optimizer -> **problem size** -> KEPT/REVERTED lookup derived from `pgo-ledger.md`,
instead of reading the full ledger text every invocation. Skip a finding only when its
kernel+optimizer pair is indexed as `REVERTED` **at the same problem size `benchmarks.json`'s
`runCmd` currently measures at** — a REVERTED verdict recorded at one size (e.g. bfs's default
`graph1MW_6.txt`) does not cover a different size (a bigger graph, a `-s large` flag, etc.); don't
carry that blackout across sizes. Entries logged before problem-size tracking existed (2026-08-08)
land under the literal key `"(unknown — predates problem-size tracking)"` — treat those as
matching only the size the benchmark is *currently* configured at, not every size, since that's
almost always what they were actually measured at historically. Don't re-attempt something already
known not to work at the size in play, and don't re-derive that decision by scanning conversation
history. `pgo-ledger.md` remains the source of truth; the index is always rebuilt from it and never
hand-edited. If a ledger entry can't be attributed to a known kernel+optimizer pair (the script
logs this to stderr when it happens), or if the index script is ever unavailable, fall back to
reading `pgo-ledger.md` directly rather than skipping the check.

**Deliberately retesting a REVERTED entry at a larger problem size** (e.g. to check whether an
optimization that didn't pay off at the default size pays off at scale) is a legitimate, expected
use of this skill — see [references/design-history.md](references/design-history.md)'s Phase 2 entry for the method: temporarily point
`benchmarks.json`'s `runCmd` at a larger dataset/size argument, run `baseline` fresh at that size,
apply the change, run `compare`, then restore `runCmd`. The resulting ledger entry's `problemSize`
line will differ from the original REVERTED entry's, so both stay in the ledger side by side —
never edit or delete the original entry to make room for a new verdict.

### 3.5. Consult the cross-benchmark prior

```
python3 external/gpa-agent-skill/scripts/build_global_index.py
python3 external/gpa-agent-skill/scripts/get_optimizer_prior.py <benchmark>
```

The first command aggregates all benchmarks' ledger indexes (step 3's per-benchmark view) into
one cross-benchmark view keyed by **(optimizer, dominant stall reason)** — not optimizer alone,
since 2026-08-09 (Phase 4) — at `state/global-optimizer-index.json`. Optimizer-only was a
popularity count ("`GPUWarpBalanceOptimizer`: 1 KEPT/0 REVERTED") that couldn't distinguish a
genuinely similar bottleneck from a coincidentally-same optimizer class applied somewhere
unrelated; stall reason (GPA's own `GINS:LAT_*` code, from `finding["dominantStallReason"]`) makes
it "has this optimizer worked on a bottleneck that looks like *this one*?" instead. It's rebuilt
from the tracked `pgo-ledger.md` files every time and skips the write when nothing changed, so
running it is always safe and cheap.

The second command joins that against this benchmark's current top findings and prints **two**
priors per candidate: `priorElsewhere.sameStallReason` (history matching both optimizer AND
dominant stall reason — the more predictive one) and `priorElsewhere.anyStallReason` (history for
the optimizer class regardless of stall reason — the old, coarser signal, kept alongside since
most ledger entries still predate stall-reason tracking and land under
`(unknown stall reason)`). Both deliberately exclude this benchmark's own history, which step 3
already covers.

Use this to **rank**, not to filter: among findings not already REVERTED on this benchmark (step
3), prefer one whose optimizer has a strong track record elsewhere — weight `sameStallReason` over
`anyStallReason` when both have data, since it's the closer match. A poor track record elsewhere is
a reason for caution, not automatic exclusion -- every benchmark's kernel is different, and an
optimizer that failed once elsewhere can still be the right fix here.

**Check `possiblyAlgorithmic` before picking a finding, not after a fix fails.** Every finding in
`profile-summary.json` carries `possiblyAlgorithmic` (bool) and `possiblyAlgorithmicNote` (string,
when true) — a mechanical flag for "this finding's shape looks like a data-layout/algorithmic
problem, not something an instruction-level optimizer can fix," set when >=90% of a finding's
blamed locations are memory-latency stalls (`GINS:LAT_GMEM*`/`GINS:LAT_TMEM*`) and the finding
itself accounts for >=50% of kernel time (see `possibly_algorithmic()` in `parse_advice.py`). This
codifies a real precedent: XSBench's material/energy lookup kernel had 93%+ of blame on an
energy-indexed global-memory gather that no `GPU*Optimizer` in this catalog could actually fix —
sorting lookups by material/energy for cache locality (the real fix) is an algorithmic
restructuring outside what a single-kernel instruction-stall profiler can propose on its own, and
it took a human noticing the concentration by hand to recognize that (2026-07-19 session).
When a top-ranked finding is flagged: still allowed to attempt the highest-`impactScore`
instruction-level optimizer on it if one exists (the flag is a caution, not a block), but say so
explicitly in your step 9 report -- name the flag, the memory-latency percentage, and that a real
fix may require restructuring data layout/access order, which is outside this skill's scope to
apply automatically. Don't silently try three instruction-level tweaks against a flagged finding
and report "didn't help" without ever surfacing that the shape predicted this.

### 4. Locate and analyze

`Read` the actual source at every `location` cited by the top (not-yet-reverted) finding. Ground
your analysis in the real code — never describe a fix for code you haven't read.

### 5. Suggest

State the specific change: what line(s), what transformation, and why it addresses the specific
stall reason (`GINS:LAT_*` etc.) the finding cited. **Quote the literal token** (e.g.
`GINS:LAT_IDEP_SMEM`), not just a paraphrase of what it means — this becomes part of the ledger's
`Change:`/`Decision:` prose, and `build_ledger_index.py` scans that text for the exact
`GINS:[A-Z_]+` pattern to populate the cross-benchmark prior's stall-reason dimension (step 3.5).
A description like "redundant shared-memory read" without the literal code is invisible to that
scan and silently falls back to `(unknown stall reason)` — true of nearly every ledger entry
written before 2026-08-09, which is why the prior's `sameStallReason` bucket starts out sparse.

### 6. Apply

Before editing, confirm the target file is clean:

```
git status --porcelain -- <file>
```

If it's not empty (uncommitted changes already present), STOP and tell the user — do not edit on
top of unknown existing changes. **Exception:** on a benchmark's very first-ever attempt right
after onboarding, `??` (untracked) is expected if the vendored source hasn't been committed yet —
that's your own just-added file, not unknown prior work, so it's safe to proceed. But note this
for step 8: `git checkout -- <file>` only works on a *tracked* file, and does nothing (silently
errors) on one that's still untracked. Commit the untouched vendored source in its own commit
*before* attempting the first optimization on a new benchmark, so step 8's revert path always has
something real to check out against — if you skip that and end up REVERTED on an untracked file
anyway, restore it by hand (re-copy from the original source, or manually undo the exact edit) and
verify the result matches the pre-edit content before moving on. Otherwise make the change with
`Edit`.

### 7. Verify correctness and benchmark

```
python3 external/gpa-agent-skill/scripts/pgo_bench.py compare <benchmark>
```

This rebuilds, re-runs, re-profiles, checks correctness against the baseline, prints
end-to-end and per-kernel speedups, and appends a **facts-only** entry to
`<benchmark-dir>/gpa-database/pgo-ledger.md` (correctness verdict + measured speedups — no
judgment call).

**Local (per-kernel) timing via nsys is best-effort, and on WSL2 it's usually just broken.**
`nsys`'s CUPTI Activity kernel-trace capture has a confirmed, still-unresolved gap under WSL2 (NVIDIA
staff acknowledged this on the developer forums; it slipped several planned releases and is still
reported broken as of early 2026) — it can fail cleanly (empty `kernelTimesNs`, a `localTimingNote`
explaining why) or hang until its own 180s timeout, handled the same way either way. If
`kernelSpeedups` comes back empty, do not invent a kernel-level number — fall back to end-to-end,
citing `localTimingNote`, and check the next paragraph first.

**Prefer `selfReportedKernelSpeedups` over nsys when a benchmark has one.** Some benchmarks already
wrap their own kernel launch with `chrono`/`cudaEvent`/`gettimeofday` and print the result — that
number has nothing to do with nsys and isn't subject to the WSL2 gap at all. `benchmarks.json`'s
optional `localTiming` list (`{label, regex, unit, occurrence?}` entries) tells `pgo_bench.py` how to
parse it into `selfReportedKernelTimesNs`/`selfReportedKernelSpeedups`, computed and logged
alongside `kernelSpeedups` whenever present. Currently configured for every benchmark: huffman,
gaussian, lud, srad, b+tree, xsbench, hotspot, kmeans, nw (confirmed working end-to-end
2026-07-19; b+tree's two kernels share an identical printed label, disambiguated via
`occurrence: 1`/`2`, in call order), and bfs (added 2026-08-09 — Kernel and Kernel2 alternate every
iteration of a variable-length do-while traversal loop, so they're timed as one combined
cudaEvent span across the whole loop rather than per-launch, to avoid a per-iteration host sync
distorting both the measurement and end-to-end time on this launch-overhead-dominated benchmark).
A benchmark's `localTiming` labels aren't always 1:1 with individual kernel names — some (e.g.
gaussian's `Fan1+Fan2 (combined)`) report a total across multiple kernel launches, or (lud's
`lud_cuda (kernel+memcpy)`) include memory-transfer time alongside the kernel — always read the
label, don't assume it isolates exactly one kernel's own time.

### 8. Apply policy, then log your decision

**End-to-end alone is not reliable enough to govern this decision.** Confirmed in both directions
on 2026-07-19: xsbench's end-to-end showed a 0.951x "regression" that `selfReportedKernelSpeedups`
proved was actually a 1.000x no-op (nvcc silently declined to unroll a runtime-variable-trip-count
loop); gaussian's end-to-end showed a 1.018x "improvement" that `selfReportedKernelSpeedups` proved
was actually a 0.739x kernel-level regression (a redundant-load fix that likely cost more in
register pressure than it saved). Trusting end-to-end alone would have gotten *both* of those
backwards. Determine the **governing speedup** with this precedence, per kernel/label you actually
edited:

1. `selfReportedKernelSpeedups` entry for it, if present.
2. Else `kernelSpeedups` (nsys) entry for it, if present.
3. Else `endToEndSpeedup` — and say so explicitly in the ledger (`no kernel-level signal available
   — decision based on end-to-end only`), since this is the weakest signal, not the default one.

If the matching label is a combined/impure metric (step 7's caveat — e.g. gaussian's
`Fan1+Fan2 (combined)`, lud's `lud_cuda (kernel+memcpy)`), it still outranks end-to-end, but name it
as combined/impure in the ledger so a future reader doesn't mistake it for a clean single-kernel
number.

**Check the governing metric's significance tag before trusting its direction.** `pgo_bench.py`
computes stddev across `runs` (default 3) for both end-to-end and self-reported kernel timing, and
tags every speedup `[clears 2σ]`, `[NOT SIGNIFICANT, within 2σ noise]`, or `[n<2 samples...]` in the
ledger line (see `is_significant()` / `SIGNIFICANCE_MULTIPLE`). A speedup that looks real
(1.05x-1.10x is common) but is tagged NOT SIGNIFICANT means the run-to-run noise is large enough
that the same magnitude of movement could occur with no real change at all — added 2026-08-08 after
srad's originally-KEPT 4.2% self-reported speedup (2026-07-19) turned out, on a clean N=3-vs-N=3
re-measurement, to be indistinguishable from noise (see srad's `pgo-ledger.md` for the full
before/after). This does not automatically flip a decision to REVERTED — a NOT SIGNIFICANT result
with PASS correctness and no significant regression either is genuinely ambiguous, not evidence of
harm — but it must be stated plainly in the ledger, not glossed over as a confirmed win.

- If correctness is **FAIL**: revert immediately with `git checkout -- <file>`, then append a line
  to the ledger: `- Decision: REVERTED — correctness failed (<detail>)`. Stop and report. Do not
  retry the same change.
- If correctness is **PASS** but the governing speedup is **< 1.0 and significant**: revert the
  same way, log `- Decision: REVERTED — regression (Nx <governing metric name>, clears 2σ)`, even
  if `endToEndSpeedup` alone would have looked like an improvement (see gaussian's case above).
  Stop and report.
- If correctness is **PASS**, the governing speedup is **< 1.0 but NOT significant**: do not revert
  on this number alone — say so explicitly (`- Decision: KEPT (unconfirmed) — nominal Nx
  regression on <metric>, does not clear 2σ, correctness holds`) and prefer more repeat runs over a
  snap judgment either way.
- If correctness is **PASS** and the governing speedup is **>= 1.0 and significant**: keep the
  change, log `- Decision: KEPT — <governing speedup, plus the other available numbers for
  context>`.
- If correctness is **PASS** and the governing speedup is **>= 1.0 but NOT significant**: keep the
  change only if it's a real code-quality improvement independent of the unproven timing win (e.g.
  provably removes redundant work), and log `- Decision: KEPT (unconfirmed) — <nominal speedup>,
  does not clear 2σ at n=<runs>`. Do not claim this as a validated performance win in any
  higher-level summary (a project report, papers.md, etc.) without a larger-n re-test.
- One optimization attempt per invocation unless the user explicitly asks you to continue to the
  next finding. If continuing to the next finding, go back to step 0 — the profile must be
  regenerated against the code as it now stands before picking the next target.
- Never edit a file outside the target benchmark's directory.
- Never skip step 7 to report a speedup number — only report numbers a script actually printed.

### 9. Report

Summarize for the user: what changed (file:line, one-sentence why), the correctness verdict, which
speedup governed the decision and why (step 8's precedence), the other available numbers for
context, and the decision you logged.

## Sweep mode

When asked to run the optimization loop across multiple benchmarks (or "all" of them) in one
sitting, loop over `assets/benchmarks.json`'s entries in order. For each benchmark:

1. **Check for a cheap exhaustion verdict first, without re-profiling.** If
   `<benchmark-dir>/gpa-database/profile-summary.json` exists, run steps 3 (ledger index) and 3.5
   against its *already-cached* findings only -- do not regenerate `gpa.advice` for this check. If
   every one of those cached top findings is already `REVERTED` on this benchmark, this benchmark
   is exhausted: skip steps 0-8 entirely, run
   `python3 external/gpa-agent-skill/scripts/log_sweep_result.py <benchmark> --status exhausted`, and
   move to the next benchmark. (This verdict is only as fresh as the cached profile-summary.json --
   if the benchmark's source has changed since it was generated, delete that file to force a real
   recheck on the next sweep.)
2. **Otherwise, run the full loop (steps 0-8) normally** for this benchmark, including the fresh
   profile regeneration step 0 always requires. The Apply step (6) still goes through normal Edit
   tool approval -- sweep mode does not skip or batch that confirmation, it only automates the
   deterministic steps around it.
3. **After step 8's decision is logged to `pgo-ledger.md`**, also record it in the cross-sweep
   audit trail:
   ```
   python3 external/gpa-agent-skill/scripts/log_sweep_result.py <benchmark> --status attempted \
       --kernel <kernel> --optimizer <optimizer> --decision <KEPT|REVERTED> \
       --e2e-speedup <value if PASS, omit if not>
   ```
4. Continue to the next benchmark. One optimization attempt per benchmark per sweep, same as the
   single-benchmark policy in step 8.

At the end, summarize the whole sweep from `state/sweep-log.jsonl` (a `tail -n <count>` of the
lines just appended) rather than re-reading every benchmark's `pgo-ledger.md` -- that file exists
specifically so a multi-benchmark run is reviewable in one small pass.

## Files in this skill

- `scripts/run_gpa_profile.py` — runs the full `hpcrun -> hpcstruct -> hpcprof` pipeline for a
  benchmark and (re)writes `gpa-database/gpa.advice`, merging its output into `gpa-database/`
  without touching the ledger/baseline files that also live there. Each subprocess's full output
  is captured to `gpa-database/pipeline-logs/<n>-<step>.log`; only a one-line pass/fail per step
  (plus the log tail on failure) is printed.
- `scripts/parse_advice.py` — parses/dedupes/ranks `gpa.advice` into JSON (see
  [references/gpa-advice-format.md](references/gpa-advice-format.md) for the format it reads).
- `scripts/get_profile_summary.py` — cached wrapper around `parse_advice.py`, keyed by
  `gpa.advice`'s content hash; persists to `gpa-database/profile-summary.json`. Optional
  accelerant, not a hard dependency — falls back cleanly to calling `parse_advice.py` directly.
- `scripts/build_ledger_index.py` — derives `gpa-database/pgo-ledger-index.json` (a
  kernel+optimizer -> KEPT/REVERTED lookup) from `pgo-ledger.md`, keyed by the ledger's content
  hash. Optional accelerant, not a hard dependency — `pgo-ledger.md` remains the source of truth.
- `scripts/pgo_bench.py` — builds, runs (median of N for end-to-end wall-clock), profiles with
  `nsys` for per-kernel time (best-effort — see the WSL2 caveat in step 7), checks correctness
  (stdout marker or output-file diff, per `assets/benchmarks.json`), and computes speedups.
  `baseline` captures reference state; `compare` measures after a change and updates the ledger.
  Also parses `assets/benchmarks.json`'s optional `localTiming` entries out of the benchmark's own
  stdout (`parse_local_timing()`) into `selfReportedKernelTimesNs`/`selfReportedKernelSpeedups` —
  an nsys-independent kernel-timing source for benchmarks that self-report one.
- `scripts/build_global_index.py` — aggregates every benchmark's ledger data into one
  cross-benchmark view keyed by optimizer, at `state/global-optimizer-index.json`. Always fully
  rebuilt from the tracked `pgo-ledger.md` files (never hand-edited); write is skipped when the
  combined content hash is unchanged.
- `scripts/get_optimizer_prior.py` — joins `state/global-optimizer-index.json` against a single
  benchmark's current top findings, returning a small `priorElsewhere` annotation per candidate
  (has this optimizer worked on *other* benchmarks?). Depends on both `profile-summary.json` and
  `state/global-optimizer-index.json` already existing.
- `scripts/log_sweep_result.py` — appends one compact line to `state/sweep-log.jsonl` per
  benchmark processed during a sweep (see "Sweep mode" above). Pure append, never rewritten —
  `pgo-ledger.md` remains the source of truth for the decision itself.
- `assets/benchmarks.json` — per-benchmark config: directory, build/run commands, correctness
  check mode, target kernel names.
- `assets/ledger-template.md` — header used when a benchmark's ledger file is created for the
  first time.
- `state/global-optimizer-index.json` — generated, gitignored (see `state/.gitignore`); rebuild
  any time with `build_global_index.py`.
- `state/sweep-log.jsonl` — generated but tracked in git (not regenerable — see
  `state/.gitignore`); the durable audit trail of every sweep run.
- `scripts/_gpa_root.py` — resolves `$PW_GPA_ROOT` / `$PW_GPA_TOOLCHAIN` (see Setup at the top),
  falling back to the pre-extraction "three levels up" guess. Added during extraction from
  ML-PGO; not part of the original skill.
- `scripts/check_coverage.py` + `assets/known-coverage.json` +
  [references/coverage-check-methodology.md](references/coverage-check-methodology.md) — a
  *research* aid, orthogonal to the optimization loop: "is this candidate benchmark already
  covered by GVProf / DrGPUM / GPA in the literature?", used when onboarding a new benchmark
  surfaced by a paper survey. Snapshot dated 2026-07-17; refresh before trusting a `not-found`.
- [references/design-history.md](references/design-history.md) — the ML-PGO working report,
  vendored during extraction. It is the design record for the rules that look arbitrary
  otherwise: Phase 1 (2σ significance gate), Phase 2 (problem-size namespacing), Phase 4
  (stall-reason prior), Phase 5 (`possiblyAlgorithmic`). Read it before changing those rules.
