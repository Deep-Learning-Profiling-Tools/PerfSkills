# Migration: from a fabricated CSV schema to real DrGPUM output

## The problem

Every analysis path in this repo was built on this profile format:

```csv
kernel_name,runtime_ms,sm_utilization_%,memory_bandwidth_%
matmul_kernel,50.0,45,85
```

**DrGPUM does not produce that format, and cannot.** DrGPUM is a *memory*
profiler ([ASPLOS '23](https://doi.org/10.1145/3582016.3582044)). It records
allocation, transfer, access, and free events against memory objects. It has no
kernel timer, no SM-occupancy counter, and no bandwidth counter. The three
columns after `kernel_name` were invented, and so was every sample CSV in the
repository.

Consequences before this change:

- The `MEMORY_BOUND` / `COMPUTE_BOUND` / `LAUNCH_OVERHEAD` classifier was
  thresholding numbers that no profiler had produced.
- Nothing in the pipeline had ever been run against a real profile.
- Any speedup reported by the benchmark-validator path was arithmetic on
  synthetic input.

**Probable origin.** DrGPUM writes a file called `memory_liveness.csv`. It has a
`.csv` extension but contains a text trace:

```
memory_peak_kernel: 214
optimal_memory_peak: 402653184 B
current_memory_peak: 1140850688 B

op_id: 10, ALLOC 1000
op_id: 12, COPYT 1000
```

Reading that filename as tabular data, then inventing plausible column names for
a GPU profiler, produces exactly the schema that was here.

## What DrGPUM actually writes

From `gvprof -v -e memory_liveness ./app`, into an output **directory**:

| File | Format |
|---|---|
| `memory_liveness.txt` | `[<object_id>]: <op_id>(<op_enum>), ...` |
| `memory_size_list.txt` | `op_id=<object_id>, size=<bytes>` |
| `kernel_list.txt` | `<op_id>, <ctx_id>` |
| `memory_liveness.csv` | peak summary + `op_id: <n>, <OP> <ctx_id>` (not CSV) |
| `memory_growth_sequence.txt` | peak summary + running-total timeline |
| `operation_stream_info.txt` | trace annotated with CUDA stream ids |

Op enum: `0 ALLOC · 1 SET · 2 COPYT · 3 COPYF · 4 ACCESS · 5 FREE · 6 SUBALLOC · 7 SUBFREE`

Every format above was derived from the profiler source
(`profiler-enhanced/redshow/src/analysis/memory_liveness.cpp`, the `output_*`
functions, and the `memory_operation_t` enum in the matching header) rather than
inferred from sample output.

## What replaces it

```
agent/drgpum/
  parser.py      real format parser -> Profile / MemoryObject / MemoryOp
  detectors.py   six inefficiency detectors, each with evidence/harm/fix
  report.py      text and JSON rendering
  __main__.py    CLI
tests/
  make_fixture.py   generates a format-accurate synthetic profile
  test_drgpum.py    24 tests, no GPU required
```

Usage:

```bash
python -m agent.drgpum <gvprof_output_dir> --json findings.json
```

### Detectors

| Kind | Condition | Fix |
|---|---|---|
| `NEVER_USED` | ALLOC with no ACCESS/SET/COPY ever | delete the allocation |
| `LATE_FIRST_USE` | long gap ALLOC → first use | sink allocation to first use |
| `DELAYED_FREE` | long gap last use → FREE | free at last use |
| `NEVER_FREED` | ALLOC with no FREE | explicit free; check for accumulating refs |
| `REDUNDANT_H2D_COPY` | COPYT overwritten by COPYT with no read between | hoist or drop the transfer |
| `PEAK_MEMORY_GAP` | `current_memory_peak > optimal_memory_peak` | whole-run budget |

### Measuring harm

DrGPUM emits no timestamps, so idle spans are counted in **kernel launches**,
the only unit of progress in the trace. Harm is reported as **idle
byte-kernels** — bytes resident × kernel launches during which those bytes went
untouched — the discrete analogue of the memory×time integral behind DrGPUM's
own `optimal_memory_peak`. `severity` normalises that against the run's total
byte-kernel budget so findings are comparable within a run.

## Removed

- `agent/helpers/parse_drgpum_csv.py` — replaced by a shim that raises
  `NotImplementedError` with an explanation. Delete once nothing imports it.
- `agent/examples/example_baseline.csv`, `example_optimized.csv` — synthetic
  data in the fabricated schema.

## Migrated

- `agent/drgpum/` — new, the real parser and detectors
- `.claude/skills/drgpum-memory-pgo/SKILL.md` — rewritten for the real workflow
- `agent/ml_pgo_agent.py` — rebuilt on `agent.drgpum`; three stages
  (analyze / suggest / validate), Claude interprets findings but never
  supplies metrics
- `.claude/skills/{drg-profile-analyzer,optimization-suggester,benchmark-validator}/SKILL.md`
- Skills restructured to the layout on the project slides (`SKILL.md` + `scripts/` + `references/` + `assets/`), each with a
  Rationalizations section listing the excuses for skipping a step and why
  each is wrong � drawn from mistakes actually made on this project.

## Removed rather than ported

Everything below consumed the fabricated schema end to end. None of it could be
translated, because the inputs it was written against do not exist — porting
would have meant rewriting each module from scratch anyway. All of it remains
in git history.

| Removed | Why |
|---|---|
| `agent/ml_pgo_agent_enhanced.py` | `KernelMetrics` built entirely on sm/bandwidth |
| `agent/memory_system.py` | pattern library keyed on sm/bandwidth thresholds |
| `agent/token_tracker.py` | compression assumed kernel-timing rows |
| `agent/example_complete_workflow.py` | hardcoded fake metrics |
| `agent/research_runner.py` | batch driver over the CSV path |
| `agent/helpers/parse_drgpum_csv.py` | the parser itself |
| `agent/examples/*.csv`, `pgo_results.json` | synthetic data in the fake schema |
| `agent/{README,QUICKSTART,INTEGRATION_GUIDE,REBUILT_ARCHITECTURE,REBUILD_SUMMARY*}.md`, `IMPROVEMENTS.md`, `MERGE_SUMMARY.md`, `SKILL_ENHANCED_README.md`, `UNIFIED_USAGE.md` | ~4,000 lines documenting the pipeline that never existed |

`memory_system.py` deserves a note. Its purpose — the agent-memory design on the
project's slides (ledger / view / policy) — is real and assigned. Only its
*implementation* was unusable, being keyed on SM-utilization and bandwidth
buckets. That concept is now implemented properly in **`agent/ledger.py`**: an
append-only record keyed on finding kind, object id, and measured peak delta,
with the termination clauses from `.claude/skills/drgpum-memory-pgo/SKILL.md` enforced in
`Ledger.should_stop()`.

## Fixtures are synthetic until a real capture exists

`tests/fixtures/synthetic_run/` is format-accurate but the numbers are invented.
It exists so the pipeline is testable without a GPU. **It is not evidence.**
Replace it with a real capture before reporting any result:

```bash
gvprof -v -e memory_liveness ./app
# copy the output directory into tests/fixtures/real_<appname>/
```
