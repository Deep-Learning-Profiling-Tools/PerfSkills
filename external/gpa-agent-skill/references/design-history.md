# ML-PGO / GPA — Working Report

Running log of changes made to the PGO-skill agent and its benchmark corpus, in
chronological order. Each entry: what changed, why, and what was found. This
file is the durable record — session notes in `MyProject/CLAUDE.md` summarize,
this file has the detail.

---

## 2026-08-08

### Entry 0 — Housekeeping: commit prior instrumentation, drop XSBench experiment

**Why:** Before starting the 5-phase improvement plan below, the working tree
had three weeks of uncommitted state from the 2026-07-19 "skill vs. human"
session: validated cudaEvent instrumentation for hotspot/kmeans/nw, ledger
entries recording that session's retests, and an explicitly-held-off XSBench
material-sort experiment. Needed to resolve this before Phase 1/3 touched the
same files.

**Changes:**
- `GPA-Benchmark` submodule commit `4aa7994`: committed cudaEvent kernel
  timers for `hotspot.cu`, `kmeans_cuda.cu`, `needle.cu`, plus the
  corresponding `pgo-ledger.md` entries for b+tree, gaussian, hotspot, kmeans,
  lud, nw recording the "-opt reference technique retested on this hardware"
  results from that session.
- `GPA-Benchmark` submodule commit `10344d5`: added `.gitignore` patterns for
  `*.qdstrm`, `*.out`, `*.pgm`, `result.txt`, and four named compiled binaries
  (`bfs`, `needle`, `pavle`) — scoped to not affect already-tracked fixtures
  (e.g. `srad_v1-opt/image_out.pgm` stays tracked; only future untracked
  files are hidden from `git status`).
- Discarded (`git checkout --`) the uncommitted XSBench material-sort
  experiment (`Main.cu`, `Simulation.cu`, `XSbench_header.h`, +218 lines) —
  per 2026-07-19 notes, correctness held but delivered no measurable
  speedup, and the user confirmed dropping it rather than committing.
- Main repo commit `9a92bb8`: `benchmarks.json` `localTiming` config for
  hotspot/kmeans/nw (companion to the cudaEvent timers above) + submodule
  pointer bump.

**Findings (carried over from 2026-07-19, now committed to the ledger):**
Of the 4 direct `-opt`-reference-technique retests done that session (b+tree
findRangeK, gaussian x2, lud), only **0** held up as real wins — all
regressed or failed correctness on this RTX 3070. Full numbers are in each
benchmark's `pgo-ledger.md`; this is the corpus underlying the "5 of 6
historical `-opt` techniques didn't transfer" finding that motivates Phase 2
below.

**Left alone, out of scope:** `data/` + `data.tar.gz` (1.1GB + 376MB,
untracked — likely a downloaded dataset, not touched); `hpctoolkit`
submodule showing as untracked in `git status` despite being a registered
gitlink (pre-existing oddity, unrelated to GPA-Benchmark, not investigated).

---

### Entry 1 — Phase 1: statistical rigor (N-repeat + stddev significance gate)

**Why:** the ledger's KEPT/REVERTED verdicts came from a point comparison — end-to-end was
already medianed across `runs` (default 3), but self-reported kernel timing was parsed from only
the *last* run's stdout, a single sample. No stddev was recorded anywhere, so a verdict like
srad's 4.2% self-reported speedup had no way to be checked against measurement noise.

**Changes** (`.claude/skills/PGO-skill/scripts/pgo_bench.py`, commit `8ae217b`):
- `timed_runs()` now returns per-run times/stdouts instead of pre-collapsing to a median.
- Self-reported kernel timing is now parsed from *every* run's stdout and reduced to
  median/stddev/n per label, same as end-to-end.
- New `is_significant(base_val, base_sd, cur_val, cur_sd)`: a delta counts as real only if it
  clears `SIGNIFICANCE_MULTIPLE` (2.0) pooled-stddev units (`sqrt(sd_before^2 + sd_after^2)`).
  Returns `None` (not True/False) when there's no variance estimate on one side (n<2) — treated
  as "unverified," not silently passed or failed.
- Every ledger speedup line is now tagged `[clears 2σ]`, `[NOT SIGNIFICANT, within 2σ noise]`, or
  `[n<2 samples...]`.
- `SKILL.md` step 8's decision policy now branches on significance as well as direction: a
  speedup that doesn't clear 2σ gets logged as `KEPT (unconfirmed)` / `REVERTED` is reserved for
  *significant* regressions only, not any negative-looking nominal number.

**Retest — srad's 2026-07-19 KEPT 4.2% verdict** (chosen because the user specifically flagged it
as close enough to noise to warrant a repeat): the original 2026-07-19 measurement compared a
3-sample "after" against a 1-sample "before" (the baseline predated this instrumentation), so it
wasn't a clean test even by the new code's own standard. Re-ran it properly: temporarily checked
out `srad_kernel.cu` at its pre-fix commit (`25361e2^`), captured a fresh 3-sample baseline with
the new stddev-aware code, restored the KEPT fix (`25361e2`), and re-ran `compare` — a true
N=3-vs-N=3 comparison.

**Finding: does not hold up.** All three self-reported metrics (extract, compress, combined
COMPUTE stage) point the same direction (5-9% faster) with clean matched samples, but **none
clears the 2σ gate**. The combined-COMPUTE-stage metric that originally justified KEPT came
closest — diff 1,186,000ns vs. a 1,303,600ns threshold, ~91% of the way there — but still falls
short. Notably, `compress`'s original dramatic-looking 2.366x swing (from the mismatched-sample
comparison) collapsed to a mundane, also-not-significant 1.054x once measured against a same-N
baseline — direct proof that the apparent "significance" there was a sample-size artifact, not a
real effect. Correctness held (byte-identical output) both times, and no metric showed a
*significant* regression either, so the ledger verdict was downgraded to **KEPT (unconfirmed)**
rather than flipped to REVERTED — full numbers in
`GPA-Benchmark/rodinia/srad/srad_v1/gpa-database/pgo-ledger.md` (commit `e77a9bf`).

**Open item flagged, not yet actioned:** if srad's kernel-level number ever needs to be
load-bearing for a paper claim, it should be re-measured at n=10+ — 3 samples catches gross
regressions but isn't enough to confidently confirm a ~5-9% effect against ~2-3% per-run relative
noise on this hardware.

---

### Entry 2 — Phase 2: problem-size namespacing

**Why:** the ledger conflated "doesn't help" with "doesn't help at this problem size" — every
REVERTED verdict was measured at whatever size `runCmd` used for turnaround time, and the
ledger-index's "skip anything already REVERTED" logic (used by SKILL.md step 3 to avoid
re-proposing dead ends) applied that verdict as a blanket skip with no size dimension at all.

**Changes** (commit `331ba02`):
- `pgo_bench.py`: new `problem_size_fingerprint(cfg)` derives a deterministic label from
  `runCmd`'s own arguments — not free-text prose an agent might describe inconsistently — recorded
  in `pgo-baseline.json` and as a `- Problem size: ...` ledger fact line. `compare` notes inline
  when baseline/current sizes differ, so a deliberate different-size comparison is never mistaken
  for a same-size regression check.
- `build_ledger_index.py`: index restructured from `kernel -> optimizer -> {decision}` to
  `kernel -> optimizer -> problemSize -> {decision}`. Pre-existing entries (no Problem size line)
  land under the literal key `"(unknown — predates problem-size tracking)"` rather than being
  dropped or merged into a real bucket.
- `build_global_index.py` / `get_optimizer_prior.py`: updated to carry the extra nesting level
  through the cross-benchmark aggregation and prior lookup. Verified end-to-end by rebuilding every
  benchmark's ledger index plus the global index and spot-checking `get_optimizer_prior.py huffman`.
- `SKILL.md` step 3: skip logic is now "REVERTED at the size currently configured," not a blanket
  skip; documents the retest-at-larger-size workflow.
- Fixed a real bug this surfaced: srad's Phase 1 ledger entry used `**KEPT` (bold markdown) in its
  Decision line, which silently broke `DECISION_RE` and dropped that entry from the index entirely
  — reworded to plain `KEPT (unconfirmed)` (commit `7a9373d`).

**Retests — nw and lud's REVERTED fixes at 4x problem size** (chosen as the two of the "5 of 6
historical `-opt` techniques didn't transfer" cohort with clean before/after kernel-level data).
Neither fix had ever been committed to git (each was applied, measured, and `git checkout --`'d
within the same historical session), so both were reconstructed from the ledger's own prose
description rather than `git apply`'d from a saved diff. Method for both: save the reconstructed
fix as a patch, revert to original source, temporarily bump `benchmarks.json`'s `runCmd` to a
larger size, capture a fresh baseline, reapply the patch, run `compare`, then restore source,
config, and baseline/golden files to their committed defaults.

- **nw** (`needle_cuda_shared_2`'s matrix_cuda-prefetch reorder, 2048 → 8192 linear size): was a
  confirmed ~10% regression (0.904x) at the default size — flattens to a statistically flat,
  NOT SIGNIFICANT 1.006x at 4x size. The regression's *visibility* fully reverses at scale.
- **lud** (`lud_diagonal`'s register-accumulator hoist, 256 → 1024 linear size): regression shrinks
  from -20.7% (0.793x, looked like a clear signal) at default size to a nominal, NOT SIGNIFICANT
  -6.8% (0.932x) at 4x size — same direction as nw's result (consistent with a roughly fixed
  per-launch overhead that dilutes as more real work runs per kernel invocation) but weaker: it
  never actually flips to flat/positive, just shrinks.

**Neither retest reverses a REVERTED verdict** — both original entries stand unmodified for their
original problem size, and neither new result clears 2σ significance in the positive direction
either, so nothing was proposed as KEPT at any size. But both are concrete, measured evidence for
the exact concern that motivated this phase: a REVERTED verdict recorded at a small problem size
is not proof an optimization "doesn't work" — only that it doesn't help *at that size*. Full
numbers in each benchmark's `pgo-ledger.md` (commit `7396e3f`).

**Not yet done:** only 2 of the 5-6 REVERTED `-opt`-technique entries were retested at scale
(nw, lud). b+tree's findRangeK REVERTED entry and gaussian's two REVERTED entries were not
retested this session — worth doing before treating the corpus-wide "didn't transfer" conclusion
as fully re-validated at scale rather than just partially spot-checked.

---

### Entry 3 — Phase 3: bfs self-reported kernel timing

**Why:** every other benchmark already had kernel-level signal (directly, or retrofitted in
Phase 1's housekeeping for hotspot/kmeans/nw) — bfs was the sole remaining benchmark relying on
end-to-end timing alone, a real gap given how often `selfReportedKernelSpeedups` has caught cases
where end-to-end was misleading (xsbench, gaussian, srad, all documented in earlier sessions).

**Change** (`GPA-Benchmark` commit `d7292f3`, main repo commit `131aed4`): added a cudaEvent-based
timer to `bfs.cu` spanning `Kernel` and `Kernel2` — which alternate every iteration of a
variable-length `do`/`while` traversal loop (iteration count is graph-dependent, already reported
as `"Kernel Executed %d times"`) — as **one combined span across the whole loop**, not per-launch.
Timing each launch separately would need a `cudaEventSynchronize` inside the loop body every
iteration; for a loop this launch-overhead-dominated, that host sync would distort both the
measurement and end-to-end time (the instrumentation is always-on, not profiling-only). Same
"combined across multiple kernels" convention already established by gaussian.cu's `Fan1+Fan2`
timer. Wired into `benchmarks.json` as `"Kernel+Kernel2 (combined)"`.

**Verification:** fresh `pgo_bench.py baseline bfs` run — correctness PASS, kernel timing parses
correctly (2,636,064ns ± 131,056ns, n=3). SKILL.md's step 7 paragraph updated: self-reported timing
now covers all 10 configured benchmarks, not 9 of 10.

---

### Entry 4 — Phase 4: key cross-benchmark prior on (optimizer, dominant stall reason)

**Why:** the cross-benchmark prior (`get_optimizer_prior.py`, built Phase 4 of the earlier
5-requirement plan) was keyed on optimizer class alone. `"GPUWarpBalanceOptimizer: 1 KEPT/0
REVERTED"` is a popularity count, not a predictive signal — it can't distinguish a genuinely
similar bottleneck from a coincidentally-same optimizer class applied to an unrelated stall
pattern. GPA itself already prints a specific stall-reason code per finding (`GINS:LAT_*`,
e.g. `GINS:LAT_IDEP_SMEM`) that was being parsed and then discarded.

**Changes** (commit `998db1f`):
- `parse_advice.py`: `BLAME_RATIO_RE` now captures the stall-reason token, not just the ratio.
  `build_findings()` derives `dominantStallReason` per finding from its highest-ratio blamed
  location. Two vendored copies of this file exist (`gpa-advisor/` and the skill's own
  `scripts/`) — kept in sync.
- `build_ledger_index.py`: scans ledger prose for `GINS:[A-Z_]+` (same free-text-scan approach
  already used for optimizer-class attribution) and attaches it to each indexed entry, falling
  back to `"(unknown stall reason)"` for entries that don't mention it rather than dropping them.
- `build_global_index.py`: re-keyed from optimizer-only to composite `"<optimizer>|<stallReason>"`
  buckets, with `optimizer`/`stallReason` also stored as separate fields per bucket.
- `get_optimizer_prior.py`: now returns **two** priors per candidate instead of one —
  `sameStallReason` (optimizer AND stall reason both match — the predictive signal) and
  `anyStallReason` (optimizer only — the old, coarser signal, kept alongside rather than dropped).
- `SKILL.md` step 5 now tells the agent to **quote the literal `GINS:` token** in ledger prose, not
  just paraphrase what it means in English — that's what the text scan matches on. Step 3.5
  rewritten to describe the dual-prior structure and recommend weighting `sameStallReason` higher
  when both have data.

**Finding from verification:** rebuilt every benchmark's ledger index and the global index, then
ran `get_optimizer_prior.py huffman` against real data — the dual-prior structure works correctly
end-to-end (`anyStallReason` shows huffman's `GPUWarpBalanceOptimizer` candidate has a 2 KEPT/0
REVERTED history from b+tree; `sameStallReason` correctly shows empty). But **zero existing
ledger entries currently carry a literal `GINS:` token** — grepped all 10 benchmarks' ledgers and
found exactly one incidental mention (lud's 2026-07-01 entry), and that entry isn't even
optimizer-attributed, so it doesn't index anyway. This isn't a bug: no prior session was ever
asked to cite the token, so none did — plain-English paraphrases ("redundant shared-memory
read", "block-uniform value") were the norm. `sameStallReason` will start filling in only as new
ledger entries get written under the updated step 5 instruction; it's expected to stay sparse for
a while.

---

### Entry 5 — Bonus: mechanical `possiblyAlgorithmic` flag

**Why:** XSBench's material/energy lookup kernel (2026-07-19 session) had 93%+ of blame
concentrated on an energy-indexed global-memory gather — a data-layout problem (sort lookups by
material/energy for cache locality) that no instruction-level `GPU*Optimizer` in this catalog
could actually fix. It took a human noticing the concentration by hand to recognize the pattern
and go implement a scoped material-sort experiment (which itself delivered no speedup, since the
real cost was the un-addressed energy-indexed part — see the 2026-07-19 session notes). The user's
framing: teach the agent to recognize this *shape* mechanically instead of relying on a human to
spot it every time.

**Change** (commit `3fd1656`): `parse_advice.py` gained `possibly_algorithmic(entry_ratio,
locations)` — flags a finding when >=90% of its blamed locations (summed by ratio, not just
counted) share a memory-latency stall reason (`GINS:LAT_GMEM*`/`GINS:LAT_TMEM*`) and the finding
itself accounts for >=50% of kernel time. Every finding in `profile-summary.json` now carries
`possiblyAlgorithmic` (bool) and `possiblyAlgorithmicNote` (string). No new instrumentation needed
— this is a pure analysis change on `locations[].ratio`/`stallReason` data Phase 4 already
captures. `SKILL.md` step 3.5 gained a paragraph instructing the agent to check the flag before
picking a finding and to name it explicitly in its report rather than silently trying
instruction-level tweaks and reporting "didn't help."

**Calibration note — the first threshold design was wrong, caught by testing against real data:**
the initial version flagged a finding when its single *top* location's ratio alone exceeded 70%.
Tested against XSBench's real `gpa.advice` and got zero flags, including on the exact finding that
motivated this. Root cause: GPA's own duplicate-line quirk (each Hot BLAME/address pair prints
twice) plus genuinely many nearby addresses within one gather loop means blame is naturally spread
across dozens of small-ratio locations (18.8%, 18.4%, 12.7%, ... down to 0.01%) — no single
location ever dominates, even when the *finding as a whole* is overwhelmingly memory-latency-bound.
Fixed by summing ratios by stall-reason category across all of a finding's locations instead of
looking at the top one alone: this correctly reads XSBench's flagged finding as 99.7%
memory-latency-attributable (156.938 of 157.346 summed location-ratio units), matching the
historical "93%+" figure once combined with the entry's own 93.732% kernel-time ratio.

**Verification:** re-tested against all 10 benchmarks' real `gpa.advice` files. Fires on 3
(xsbench's `GPUCodeReorderOptimizer` finding — the motivating case; bfs's `Kernel` — graph
adjacency-list traversal; nw's `needle_cuda_shared_2` — offset-indexed matrix access), 0 false
positives on the other 7 (gaussian, hotspot, b+tree, huffman, kmeans, lud, srad). bfs and nw both
being graph/DP-traversal kernels with indexed/scattered access patterns is a plausible, not
alarming, result — reasonable specificity for a first-pass heuristic, not a rubber stamp that
fires on everything.

**Not yet done:** the flag is computed and surfaced in `profile-summary.json`/SKILL.md's workflow,
but no benchmark has actually been re-run through the full skill loop with an agent consciously
checking it yet — bfs and nw's flagged findings would be reasonable candidates to validate the
instruction against in a future session (does the agent actually name the flag in its report, per
the new step 3.5 instruction, when it encounters one of these two?).

---

### Entry 6 — Full 10-benchmark regression test (user-requested)

**Why:** after Phases 1-4 and the bonus item all touched `pgo_bench.py`, the ledger-index scripts,
and `parse_advice.py`, the user asked to run every configured benchmark end-to-end as a sanity
check before continuing to Phase 5. Not a phase of the original plan — a verification pass.

**Method:** ran `pgo_bench.py compare <benchmark>` (identity comparison against each benchmark's
existing baseline, no source edits) for all 10 configured benchmarks: b+tree, bfs, gaussian,
hotspot, huffman, kmeans, lud, nw, srad, xsbench.

**Finding 1 — a real bug, caught and fixed:** lud showed a correctness **FAIL** on first run.
Root cause: `pgo-baseline-stdout.json` (lud's golden correctness reference for its `stdout-diff`
mode) is **not git-tracked**, unlike `pgo-baseline-output-*` files which are explicitly kept via
the `.gitignore` exception list. Phase 2's cleanup (`git checkout --` on the tracked
`pgo-baseline.json`) restored the numeric timing baseline correctly but had no way to restore this
untracked sidecar file, which was still holding stdout captured during Phase 2's temporary 4x-size
(`-s 1024`) retest. Fixed by recapturing a fresh baseline at the correct default size (`-s 256`);
re-verified PASS.

**Finding 2 — a systemic pattern across 6 benchmarks, not a code bug:** after the lud fix, all 10
benchmarks showed correctness PASS, but several showed "significant" speedup/regression swings on
what should have been no-op identity comparisons — most strikingly hotspot's `calculate_temp` at
0.48-0.55x (reproduced twice, GPU confirmed idle/cool via `nvidia-smi` both times, no competing
GPU processes) and b+tree's `findK` at 0.46x. Traced to exactly one common factor: every affected
benchmark (b+tree, huffman, gaussian, hotspot, kmeans, xsbench) still carried its **original
single-sample baseline from 2026-07-19/20** — captured before Phase 1's N-repeat/stddev work
existed. `is_significant()` treats a missing baseline stddev as `0.0`, so comparing today's
legitimate N=3 measurement noise against a baseline with no captured uncertainty was enough to
trip the 2σ gate on its own — the identical mismatched-sample problem Phase 1 diagnosed and fixed
specifically for srad, still present in the 6 benchmarks that were never individually refreshed.

**Fix (user approved):** re-ran `pgo_bench.py baseline` for all 6 stale benchmarks with the
current N=3 code. Re-verified: every self-reported kernel metric across all 6 now correctly shows
`sig=False` on identity comparison (hotspot's `calculate_temp` went from a reproducible "0.48x
significant" to a mundane "0.97x, not significant" against its own fresh baseline). One residual:
b+tree's *end-to-end* speedup alone still shows `sig=True` (1.149x) even post-refresh, most likely
wall-clock/file-I/O variance rather than kernel-level noise — doesn't affect SKILL.md's governing-
metric policy (kernel-level `findK`/`findRangeK` are correctly `sig=False`, and end-to-end is only
ever the fallback signal), but flagged here rather than silently glossed over.

**Side effect:** also gitignored b+tree's loose `output.txt` (regenerable per-run output, same
class as the already-ignored `result.txt`).

**Net effect:** all 10 benchmarks now have trustworthy, matched-sample (N=3-vs-N=3) baselines as
of this session. Every future `compare` against them will get an honest significance read instead
of inheriting the single-sample-baseline artifact this pass uncovered.

---

### Entry 7 — Agent vs. Human (`-opt` reference) comparison, 3 benchmarks (user-requested)

**Why:** user asked how the agent's optimization attempts compare to the human-authored `rodinia
*-opt` reference implementations that ship in the benchmark suite — end-to-end speedup,
kernel-level speedup, and code-diff size, for 3 benchmarks.

**Correction made along the way:** while gathering nw's comparison data, discovered nw's baseline
was incorrectly reported as "refreshed this session" in Entry 6 — it wasn't. Phase 2 had
temporarily pointed it at a 4x-size baseline for the scale retest, then correctly restored the
*original* 2026-07-20 single-sample baseline via `git checkout` (right move for preserving
history), but that left nw with the same stale-baseline problem as the 6 benchmarks Entry 6 fixed.
Refreshed now (`e89062b`).

**Method:** chose nw, lud, and huffman (richest cross-referenced data: all three have a real
`-opt` sibling directory, and the agent tested the same underlying technique on all three).
Built and ran each `-opt` binary directly (huffman-opt and lud-opt already print their own
cudaEvent-based kernel timers; nw-opt got one added temporarily, measured, then discarded via
`git checkout --`, never committed). For huffman specifically (the one KEPT/shipped fix), also
temporarily swapped in the pre-fix source to get a true pristine-vs-agent-fixed comparison, same
method as srad's Phase 1 retest. All numbers below are fresh, same session, same hardware.

| Benchmark | Kernel | End-to-end: Agent | End-to-end: Human `-opt` | Kernel: Agent | Kernel: Human `-opt` | Diff size: Agent | Diff size: Human |
|---|---|---|---|---|---|---|---|
| nw | `needle_cuda_shared_2` | 0.860x (n.s.) | 1.136x (n.s.) | 0.904x (reported sig., N=1 era, 2026-07-20) | 1.020x (n.s., N=3 today) | 25 lines (+16/-9) | 17 lines (+11/-6) |
| lud | `lud_diagonal` | 1.003x (n.s.) | 0.937x (n.s.) | 0.793x (reported sig., N=1 era, 2026-07-20) | 0.817x (n.s., N=3 today) | 18 lines (+13/-5) | 19 lines (+12/-7) |
| huffman | `vlc_encode_kernel_sm64huff` | 0.833x (n.s.) | 0.964x (n.s.) | 0.683x (N=3 today) / formally retested: 1.041x (n.s.) | 0.982x (n.s.) | 13 lines (+12/-1) | 28 lines (+17/-11) |

("n.s." = not significant at the 2σ gate. nw/lud Agent kernel numbers are historical single-sample
ledger entries, since those fixes were REVERTED and aren't in the current committed source to
remeasure fresh.)

**Finding 1 — human `-opt` doesn't win either, on this hardware, today.** None of the three real
reference implementations show a statistically significant kernel-level speedup when freshly
measured with N=3. nw-opt and lud-opt are both nominally *slower* than the unoptimized base
(0.82-1.02x); huffman-opt is roughly flat (0.98x). Reinforces this session's recurring theme:
`-opt` techniques validated on older GPU generations don't reliably transfer to this RTX 3070,
whether the agent applies them or the original human code does.

**Finding 2 — huffman's flagship "win" didn't survive fresh scrutiny either.** huffman was the
one case in this project flagged as a genuine, confirmed agent success (1.288x kernel speedup,
KEPT 2026-07-19). This comparison exercise's fresh N=3 remeasurement got 0.683x instead — different
enough to warrant a formal retest using the same baseline-swap method as srad's Phase 1 retest.
Result: clean N=3-vs-N=3 shows **1.041x, not significant** — nowhere near the original 28.8%
claim, which rested on a single-sample-vs-single-sample point comparison with no variance estimate
at all. Downgraded to `KEPT (unconfirmed)` in the ledger (commit `ce52958`), not reverted — no
significant regression either, and `__syncwarp()` replacing an unconditional `__syncthreads()`
once only one warp has real work remains defensible on first-principles grounds independent of the
now-unconfirmed timing claim. **This is the third case this session (after srad) where a
single-sample-era KEPT verdict didn't survive a fresh N=3 remeasurement** — worth treating as a
pattern, not a coincidence: every KEPT decision made before Phase 1's statistical-rigor work
existed should be considered provisional until formally retested.

**Finding 3 — diff size doesn't track outcome.** For huffman, the agent's fix is less than half
the size of `-opt`'s (13 vs 28 lines) via a structurally different mechanism (one conditional
branch vs. splitting the loop into two with duplicated body logic) — smaller, and now measured as
roughly comparable in effect (both not-significant). For nw/lud, the agent's reconstructed diffs
run slightly *larger* than `-opt`'s own, mostly because `-opt` hoists into local scalar variables
more compactly than the agent's closer-to-original-structure approach.

**Not yet done:** srad and huffman are now both formally downgraded to `KEPT (unconfirmed)`. Three
more single-sample-era KEPT verdicts exist in the corpus and have not been retested yet, all real
candidates for the same treatment: **b+tree** (`findK`, 1.237x kernel speedup claimed),
**bfs** (`Kernel`, 1.115x end-to-end only — no kernel-level signal was available at the time),
and **hotspot** (`calculate_temp`, 1.011x end-to-end — barely above 1.0 even nominally, the most
likely of the three to not survive a retest). nw and lud's REVERTED verdicts don't need the same
treatment (both had real kernel-level signal from the start, just not stddev).

---

<!-- Phase entries appended below as work completes. -->
