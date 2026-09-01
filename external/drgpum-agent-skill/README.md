# ML-PGO: agent-guided GPU memory optimization with DrGPUM

An LLM agent skill that finds memory inefficiencies in CUDA and PyTorch
applications, explains why they cost something, proposes a source-level fix,
and verifies the fix by re-profiling.

Built on [DrGPUM](https://github.com/Lin-Mao/DrGPUM) (Lin, Zhou, Su — ASPLOS '23),
a memory profiler for NVIDIA GPUs.

## What this does, and what it does not

DrGPUM answers one question well: **which bytes are resident on the device
without earning their place there?** Everything here is built on that.

It records allocation, transfer, access, and free events against memory
objects, plus a peak-vs-optimal memory summary. It records **no kernel timing,
no SM occupancy, and no memory bandwidth** — those come from Nsight Compute,
not from this tool. Any analysis quoting those numbers from a DrGPUM run is
fabricated. See [MIGRATION.md](MIGRATION.md) for why that warning is stated so
bluntly.

## Quick start

Analysis runs anywhere — no GPU required. A real captured profile ships in the
repo:

```bash
python main.py analyze tests/fixtures/real_vectoradd --min-bytes 1
```

Full pipeline on a machine with a GPU and DrGPUM installed:

```bash
python main.py profile -- python3 train.py
python main.py analyze runs/baseline/gvprof-measurements/memory_liveness
python main.py agent   runs/baseline/gvprof-measurements/memory_liveness --source train.py
```

Then apply one fix, re-profile, and compare:

```bash
python main.py compare runs/baseline/.../memory_liveness runs/optimized/.../memory_liveness
python main.py ledger
```

`agent` calls the Claude API and needs `ANTHROPIC_API_KEY` set. Everything else
is offline.

## How it fits together

```
gvprof -e memory_liveness        →  8 plain-text files (op trace, sizes, kernels, peaks)
        ↓
agent/drgpum/parser.py           →  Profile: objects × ALLOC/COPYT/ACCESS/FREE ops
        ↓
agent/drgpum/detectors.py        →  findings, ranked by idle byte-kernels
        ↓
agent/ml_pgo_agent.py            →  Claude diagnoses, locates in source, proposes a fix
        ↓
re-profile → compare peaks       →  accept or revert; append to the ledger
```

**Detection is deterministic and local.** Every number in a report is computed
from the profiler's own bytes. Claude interprets findings and reasons about
what to change in the source; it is never the source of a metric. That
separation is the design's main constraint, and it is deliberate.

### What gets detected

| Kind | Condition | Typical fix |
|---|---|---|
| `NEVER_USED` | ALLOC, but no ACCESS/SET/COPY ever | delete the allocation |
| `LATE_FIRST_USE` | long gap ALLOC → first use | sink the allocation to first use |
| `DELAYED_FREE` | long gap last use → FREE | free at last use; narrow the scope |
| `NEVER_FREED` | ALLOC with no FREE | see the PyTorch caveat below |
| `REDUNDANT_H2D_COPY` | COPYT overwritten before any read | hoist or drop the transfer |
| `PEAK_MEMORY_GAP` | current peak vs optimal peak | the budget the others draw down |

### Measuring harm

DrGPUM emits no timestamps, so `op_id` orders events but its magnitude means
nothing. Kernel launches are the only countable unit of progress, so idle spans
are measured in **kernel launches** and harm in **idle byte-kernels** — bytes
resident × kernel launches during which those bytes went untouched. It is the
discrete analogue of the memory×time integral behind DrGPUM's own
`optimal_memory_peak`. `severity` normalises it against the run's total
byte-kernel budget, so it ranks findings *within* a run and is not comparable
across runs.

## Layout

```
main.py                     single entry point
agent/
  drgpum/                   parser, detectors, reporting  ← all analysis lives here
  ledger.py                 append-only record of what was tried
  ml_pgo_agent.py           Claude-assisted analyze → suggest → validate
  helpers/benchmark_runner.py   runs a workload under gvprof
.claude/skills/            Claude Code project skills (invoke with /drgpum-memory-pgo etc.)
  drgpum-memory-pgo/        the master skill
    SKILL.md                workflow, rationalizations, termination clauses, memory
    scripts/                executable entry points
    references/             output format, detectors, PyTorch allocator finding
    assets/                 ledger and finding-report templates
  drg-profile-analyzer/     stage 1: parse and rank
  optimization-suggester/   stage 2: locate in source, propose a fix
  benchmark-validator/      stage 3: verify and decide whether to iterate
profiler-enhanced/          vendored DrGPUM source
tests/                      30 tests; 6 against a real captured profile
benchmarks/                 real baseline/optimized program pairs; see benchmarks/README.md
MIGRATION.md                what was wrong before, and how it was fixed
```

## Building the profiler

Upstream's `./bin/install` does not complete on a modern toolchain. On Ubuntu
22.04 / CUDA 12.4 / GCC 12, these are the failures encountered and their fixes:

| Failure | Cause | Fix |
|---|---|---|
| `comparison object must be invocable as const` | GCC 11+ requires const-invocable comparators | add `const` to `StringCompare::operator()` and `FLPCompare::operator()` |
| `gotcha/gotcha.h: No such file` | gotcha 1.0.4 needs `_dl_sym`, removed in glibc 2.35 | `spack install gotcha@master` |
| `multiple definition of gpu_inst_stall_all_t` | missing `typedef` in `gpu-metrics.h` defines a *variable* in a header; GCC 10+ defaults to `-fno-common` | add the `typedef` |
| `configure: Permission denied` | exec bits lost via zip/Windows | `chmod +x configure` |

Build with `CC=gcc-12 CXX=g++-12` to match the spack-built dependencies, and
confirm `hpcrun` exists afterwards — `gvprof` is a shell script that calls it,
so a missing `hpcrun` produces a run that appears to succeed and silently
generates nothing.

## DrGPUM quirks worth knowing

Found while validating the parser against real output; all are handled in code.

- **`memory_liveness.csv` is not a CSV.** It contains `op_id: 214, ALLOC 1000`
  text lines. Parsing it as columnar data is the origin of the fabricated
  schema described in MIGRATION.md.
- **`current_memory_peak` can underflow.** `output_ctx_node()` prints
  `_current_memory_peak - 512` as unsigned 64-bit, so a run whose true peak is
  below 512 B wraps to ~1.8×10¹⁹. The vectorAdd sample reports
  `18446744073709551116` for a real peak of 12 B.
- **`optimal_memory_peak: 0` means "not computed"**, not "zero needed". Treating
  it as a value produces a bogus "100% of peak is recoverable" headline.
- **`op_id` starts at 2⁶³ + n.** Anything storing these as signed 64-bit breaks.
- **PyTorch's caching allocator holds device blocks until process exit by
  design**, so most `NEVER_FREED` findings in a torch run are the allocator
  working correctly, not leaks. An agent that does not know this produces
  confident false positives on every PyTorch program.

## Tests

```bash
python -m pytest tests/ -q
```

30 tests, no GPU needed. Six run against `tests/fixtures/real_vectoradd`, a
real capture from an RTX 3090 — those are what prove the formats were derived
correctly rather than plausibly. The `synthetic_run` fixture is
format-accurate but its numbers are invented; it exists so the pipeline is
testable off-GPU and is **not evidence of anything**.

## Benchmark suite

`benchmarks/run_suite.py` builds real baseline/optimized program pairs,
times them, checks correctness, and profiles peak memory -- producing a real
`(application, type of inefficiency, optimization, speedup)` table instead of
one filled in by hand. See [benchmarks/README.md](benchmarks/README.md).

## Reference

Mao Lin, Keren Zhou, and Pengfei Su. *DrGPUM: Guiding Memory Optimization for
GPU-accelerated Applications.* ASPLOS '23.
https://doi.org/10.1145/3582016.3582044
