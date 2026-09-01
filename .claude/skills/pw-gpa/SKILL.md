---
name: pw-gpa
description: Profile-guided optimization of CUDA kernels with GPA (GPU Performance Advisor) on HPCToolkit - analyze gpa.advice, locate the responsible source, apply one targeted optimization, verify correctness, and benchmark kernel and end-to-end speedup. Use when asked to optimize a CUDA kernel, analyze gpa.advice output, investigate a GPU performance regression, or sweep the optimization loop across every configured benchmark in one session.
allowed-tools: Read, Edit, Bash, Grep, Glob
---

# pw-gpa — GPA (GPU Performance Advisor)

Wrapper around the vendored **[gpa-agent-skill](../../../external/gpa-agent-skill/)**
(`external/gpa-agent-skill/`), which owns the nine-step optimization loop, the ledger, and the
cross-benchmark prior. This skill adds the ProfileWeaver-facing contract: setup, gate mapping,
and what the root dispatcher may conclude from a result.

**Read [external/gpa-agent-skill/SKILL.md](../../../external/gpa-agent-skill/SKILL.md) and
follow its workflow.** Everything below is the frame around it, not a replacement for it.

## Declared capability

- **Answers**: GPU instruction-stall bottlenecks — `memory-latency`, `gpu-occupancy`, `sync`,
  uncoalesced access, warp imbalance — attributed via GPA's `GINS:LAT_*` codes, each paired
  with a named optimizer and an estimated speedup
- **Cannot answer**: CPU-side cost, MPI, I/O, host↔device transfer and GPU idle gaps
  (`pw-nsight-systems` territory), or anything requiring a roofline
- **Granularity**: GPU instruction (PC) → source line, per kernel
- **Cost**: a full rebuild + profiled run per iteration, plus `nsys` for kernel timing
- **Requires**: NVIDIA GPU, CUDA toolchain, a **built** GPA/HPCToolkit install, and
  `$PW_GPA_ROOT` (see Setup). The benchmark must be registered in
  `external/gpa-agent-skill/assets/benchmarks.json`
- **Escalates to**: `pw-nsight-systems` (timeline, transfers, launch overhead),
  `pw-nsight-compute` (roofline, occupancy math)

## Setup

```bash
export PW_GPA_ROOT=/path/to/GPA   # the GPA project root: holds GPA-Benchmark/ and gpa/
```

Everything resolves **relative to that root**, matching the layout `bin/install.sh` produces:

| Needed | Resolved as |
|---|---|
| benchmark dirs | `$PW_GPA_ROOT/` + each entry's `"dir"` in `assets/benchmarks.json` (e.g. `GPA-Benchmark/rodinia/bfs`) |
| `hpcrun`, `hpcstruct`, `hpcprof` | `$PW_GPA_ROOT/gpa/hpctoolkit/bin` |
| the `gpa` driver | `$PW_GPA_ROOT/gpa/bin` |

`PW_GPA_TOOLCHAIN` overrides just the `gpa/` part, for a toolchain built outside the project
root; on a standard `install.sh` layout you do not need it. With neither set, the scripts fall
back to a pre-extraction path guess and exit with a message naming the variable.

Run all scripts **from the ProfileWeaver root**, e.g.:

```bash
python3 external/gpa-agent-skill/scripts/get_profile_summary.py <benchmark> --top 5
```

Also check `DEFAULT_GPU_ARCH` in `scripts/run_gpa_profile.py` — it is `A100`; set a benchmark's
`gpuArch` in `benchmarks.json` if your GPU differs.

**Do not edit anything under `external/gpa-agent-skill/` to work around a local problem.** It
is vendored upstream code; fixes belong there deliberately, not as drive-by edits. Two changes
were already made during extraction and are marked in place: `scripts/_gpa_root.py` (new — path
resolution) and `KERNEL_RE` in `scripts/parse_advice.py` (widened — the kernel-time field is an
integer in some GPA builds and a float in others, and matching only one silently yields
"no findings" on a valid `gpa.advice`).

## What the loop does

Nine steps, in `external/gpa-agent-skill/SKILL.md`. In outline: regenerate `gpa.advice` (0) →
baseline if none exists (1) → parse the profile (2) → **check the ledger** (3) and the
**cross-benchmark prior** (3.5) → read the source (4) → propose, quoting the literal
`GINS:LAT_*` token (5) → apply one change (6) → verify correctness and benchmark (7) → apply
the decision policy and log KEPT/REVERTED (8) → report (9).

Two properties make this child unlike the others:

- **It learns across runs.** `pgo-ledger.md` per benchmark records every verdict; a global
  index keyed by *(optimizer, dominant stall reason)* predicts whether a fix has worked on a
  similar bottleneck before. Never re-attempt something the ledger already recorded as
  REVERTED **at the same problem size** — step 3 exists for that.
- **Its significance rule is stricter than the template's.** Step 8 picks a *governing metric*
  by precedence (self-reported kernel time > `nsys` > end-to-end) and tags it `[clears 2σ]` or
  `[NOT SIGNIFICANT]`. End-to-end alone got two calls backwards in testing; do not let a report
  fall back to it silently.

## ProfileWeaver contract

This child predates `templates/child-skill/` and keeps its own loop. Map onto the shared gates
when reporting (`shared/schema/rewrite-report.schema.json`):

| Gate | Satisfied by | Notes |
|---|---|---|
| Correctness | step 7's PASS/FAIL (`stdout-marker`, `stdout-diff`, or `output-diff`) | direct match |
| Significance | step 8's `[clears 2σ]` tag on the **governing** metric | stricter than the template — record the tag verbatim |
| Mechanism | **partial** — step 0 regenerates `gpa.advice`, but nothing asserts the targeted finding shrank | to pass honestly, diff the finding's `impactScore` before/after and say so |
| No collateral | end-to-end vs. per-kernel comparison in step 7 | a kernel win with an end-to-end loss must be reported, not hidden |
| Attribution | the literal `GINS:LAT_*` token quoted in step 5 | already mandatory — it feeds the stall-reason prior |
| Reproducible | `pgo-ledger.md` entry + `benchmarks.json` config + problem size | the ledger is the source of truth |

The `mechanism` gate is the real gap: a KEPT verdict proves the code got *faster*, not that it
got faster *for the diagnosed reason*. Until the before/after finding diff is wired in, report
`status: proposed` rather than `verified` when the distinction matters — and note that
`possiblyAlgorithmic` findings are exactly where a speedup is most likely to have come from
somewhere other than the cited stall.

`limits.cannot_observe` for this child always includes: CPU-side cost, host↔device transfers,
GPU idle gaps, and — wherever `nsys` kernel trace is unavailable (it is broken under WSL2) —
per-kernel time for any benchmark that does not self-report it.
