# `gpa.advice` format reference

`gpa.advice` is GPA's (GPU Performance Advisor) plain-text profiler report, produced by the
`hpcrun -> hpcstruct -> hpcprof -> gpa` pipeline. It is large and repetitive — read this instead
of re-deriving the structure from raw text every time.

## Structure

The file is a sequence of kernel blocks, separated by `***...` lines:

```
******************************************************************************************
GPU Kernel <mangled-signature>: <index>

Code Optimizers
  Apply <OptimizerName> optimization, ratio X%, estimate speedup Yx
    <boilerplate description, fixed per optimizer name>
    1. Hot BLAME GINS:<STALL_REASON> code, ratio X%, speedup Yx
      Hot BLAME GINS:<STALL_REASON> code, ratio X%, distance N, efficiency X%, pred_true X%
        From <kernel> at <absolute file path>:<kernel-decl-line>
          <hex-addr> at Line <N> [in Loop at Line <M>]
        To <kernel> at <absolute file path>:<kernel-decl-line>
          <hex-addr> at Line <N>
--------------------------------------------------------------------------------------------

Parallel Optimizers
  ...

Binary Optimizers
  ...
```

**Important:** the `From/To ... at file:line` line gives the *kernel definition's* file and
declaration line, not the stall location. The real source line for the finding is on the
following indented `<addr> at Line N` line. Always use that `Line N`, not the decl line.

**Known quirk:** the same kernel can appear as multiple, byte-identical `GPU Kernel` blocks
(observed 8x for BFS's `Kernel2`) — this is a real artifact of repeated kernel launches, not a
parsing bug. `scripts/parse_advice.py` already dedupes these (tracked as `occurrences`).

## Ranking

Use `impactScore = (ratio / 100) * max(speedup - 1, 0)` to rank findings — this is what
`scripts/parse_advice.py --top N` already outputs, pre-sorted descending. Entries with `ratio ==
0` (no evidence) are already dropped by the parser. Entries with `speedup == null` in the JSON
correspond to `estimate speedup infx` in the raw text (occupancy-increase suggestions) — these
have no ratio-weighted evidence and are reported separately as "informational," not ranked
alongside the real findings.

## The optimizer categories (17 confirmed so far)

| Category | Optimizer | Meaning |
|---|---|---|
| Code | `GPUCodeReorderOptimizer` | Compiler isn't hiding load/dependency latency; reorder or prefetch |
| Code | `GPULoopUnrollOptimizer` | Loop-carried dependency; manual unroll or vectorize |
| Code | `GPULoopNoUnrollOptimizer` | Opposite — loop over-unrolled, bloating code/registers |
| Code | `GPUDivergeReductionOptimizer` | Warp divergence from a data-dependent branch |
| Code | `GPUGlobalMemoryReductionOptimizer` | Too many small/scattered global memory transactions |
| Code | `GPUGlobalMemoryCoalesceOptimizer` | Global memory accesses aren't coalescing across the warp |
| Code | `GPUStrengthReductionOptimizer` | Expensive instruction (int division, 64-bit conversion) with a cheaper equivalent |
| Code | `GPUWarpBalanceOptimizer` | Block-wide barrier (`__syncthreads`) paid by threads that are mostly idle (e.g. shrinking active set in a scan) |
| Code | `GPUAsyncCopyOptimizer` | Opportunity to overlap memory copy with compute |
| Code | `GPUFunctionSplitOptimizer` | Function too large/divergent, consider splitting |
| Code | `GPUIndirectAddressEliminationOptimizer` | Indirect addressing overhead that could be made direct |
| Code | `GPUFastMathOptimizer` | Precise math intrinsic used where a faster, less-precise one (`--use_fast_math` equivalents) would do |
| Code | `GPUFunctionInlineOptimizer` | Function call overhead not eliminated by the compiler; consider forcing inline |
| Parallel | `GPUOccupancyIncreaseOptimizer` / `...DecreaseOptimizer` | Active-warp count too low/high for the SM |
| Parallel | `GPUBlockIncreaseOptimizer` / `...DecreaseOptimizer` | Block/grid sizing suggestion |
| Binary | `GPURegisterIncreaseOptimizer` | Register pressure causing spills; simplify or split to free registers |

## Cached derivatives (profile-summary.json, pgo-ledger-index.json)

Two files in `gpa-database/` cache expensive-to-regenerate views of `gpa.advice`/`pgo-ledger.md`,
written by `scripts/get_profile_summary.py` and `scripts/build_ledger_index.py` respectively.
Both are strictly optional accelerants, never a source of truth:

- `profile-summary.json` — exactly `parse_advice.py`'s JSON shape (`sourceFile`,
  `kernelBlockCount`, `findings`, `informational`), plus `sourceHash` (a sha256 of the `gpa.advice`
  text it was generated from) and `top` (the `--top` value used). If `sourceHash` doesn't match
  the current `gpa.advice`, the cache is stale and `get_profile_summary.py` regenerates it
  automatically — never trust a `profile-summary.json` whose `sourceHash` you haven't checked.
- `pgo-ledger-index.json` — `{sourceFile, sourceHash, index: {kernel: {optimizer: {decision,
  lastUpdated}}}}`, where `decision` is `"KEPT"` or `"REVERTED"`. `sourceHash` is a sha256 of
  `pgo-ledger.md`'s text. An entry only appears here if `build_ledger_index.py` could confidently
  attribute it to a specific kernel (checked against `assets/benchmarks.json`'s `kernels` list for
  that benchmark) and optimizer (matched via a `GPU\w+Optimizer` pattern in the entry's text) —
  ambiguous entries are skipped rather than indexed incorrectly, and remain readable directly in
  `pgo-ledger.md`.

If either cache file is missing, deleted, or looks wrong, delete it and re-run the corresponding
script (or read the raw `gpa.advice`/`pgo-ledger.md` directly) — both scripts regenerate cleanly
from source and neither `SKILL.md` step depends on the cache surviving.

## Correlating to real source

Every finding's `locations` array (in the parser's JSON output) gives real `{file, line}` pairs.
Always `Read` the actual file at that line before proposing or applying a fix — never describe
code you have not read. The absolute paths in `gpa.advice` already point at the real `.cu` files
in this repo (e.g. `GPA-Benchmark/rodinia/bfs/kernel.cu`).

**`locations[0]` is the highest-contributing line within the finding**, not just the first one
encountered in the raw text. Each location carries a `ratio` field (the innermost `Hot BLAME ...
ratio X%` line that governs it) and `parse_advice.py` sorts locations by that ratio descending
before output. (This was a real bug until it was fixed while working on `lud`: the parser
originally sorted by `(file, line)`, which surfaced an 82.2%-ratio finding's minor contributing
line first while the true dominant blame -- individually worth more than the next several combined
-- was several `Hot BLAME` sub-entries later. Fixed by tracking each location's own governing
ratio during parsing instead of just file/line.) A `locations` entry can still lack a `ratio` if it
appeared outside any `Hot BLAME` block (rare) -- those sort last.
