---
name: pw-perf
description: Profile a CPU workload with Linux perf - sampling to find hotspots, hardware counters to explain them - then propose, benchmark, reprofile, and verify a code rewrite. Owns perf record / perf report / perf stat / perf annotate. Use for CPU-side "where does the time go", compute vs. memory-bound classification, cache and branch behavior, and scalar-vs-vector checks on x86 and Arm Linux. Not for GPU, MPI-scale, or I/O questions.
---

# pw-perf — Linux perf

Expert for **Linux `perf`**: statistical sampling plus PMU counters, no rebuild required,
overhead low enough to profile production-shaped runs. You run the ProfileWeaver loop:
**profile → normalize → optimize → benchmark → reprofile → verify → report**.

## Declared capability

- **Answers**: `hotspot`, `serial-fraction`, `compute-bound`, `memory-bandwidth` (with uncore
  events), `cache`, `vectorization` (partial, x86 via `fp_arith_inst_retired.*`)
- **Cannot answer**: GPU anything, per-rank MPI wait-state analysis, I/O-per-file behavior,
  exact cache simulation, data-structure-level locality. Hand these back to the router.
- **Granularity**: function → source line → instruction (`perf annotate`)
- **Overhead**: ~1.01–1.05× for `perf record -F 999 --call-graph fp`; up to ~1.3× with
  `--call-graph dwarf` on deep stacks; `perf stat` is ~1.0× until events multiplex
- **Requires**: Linux; `perf` on `PATH`; `perf_event_paranoid ≤ 2` to sample your own process,
  `≤ 0` (or `CAP_PERFMON`) for most counters, root for uncore/bandwidth events
- **Escalates to**: `pw-vtune` (memory-latency, top-down detail), `pw-advisor`
  (vectorization guidance), `pw-valgrind` (exact cache behavior), `pw-likwid` (pinned
  per-domain bandwidth), any `pw-nsight-*` (the moment a GPU is involved)

Read `references/tool-notes.md` before stage 2.

## Stage 1 — Preflight

```bash
perf --version
cat /proc/sys/kernel/perf_event_paranoid    # <=2 to sample, <=0 for most counters
perf list hw cache 2>/dev/null | head -40   # empty PMU list => VM/container, counters unavailable
```

- **No PMU events** (common in VMs and unprivileged containers): sampling on `cpu-clock` still
  works and answers `hotspot`, but every counter-based class is off the table. Say so; do not
  substitute intuition for the counters you could not read.
- **Symbols**: needs `-g`. For `--call-graph fp` also `-fno-omit-frame-pointer`. Confirm with
  `perf report` showing real names rather than `[unknown]` / raw addresses. `-O2 -g` is the
  right profiling build — never profile `-O0` and optimize from it, the hotspot ranking of an
  unoptimized build is a different program.
- **Baseline with variance** (≥5 reps, note `rel_mad`):
  ```bash
  python3 shared/scripts/bench.py --label baseline --reps 5 \
      --out runs/<run-id>/bench.baseline.json -- <run cmd>
  ```
- **Oracle**: confirm the correctness check passes on the unmodified build before going further.

Record everything in `runs/<run-id>/preflight.json`.

## Stage 2 — Profile

Two passes. Do the cheap one first and let it narrow the second.

**Pass A — where does time go** (answers `hotspot`):

```bash
perf record -F 999 --call-graph fp -o runs/<run-id>/raw/perf.data -- <run cmd>
# deep C++/inlined stacks without frame pointers:
perf record -F 999 --call-graph dwarf,16384 -o runs/<run-id>/raw/perf.data -- <run cmd>

perf report -i runs/<run-id>/raw/perf.data --stdio --no-children --percent-limit 0.5 \
    > runs/<run-id>/raw/report.self.txt
perf report -i runs/<run-id>/raw/perf.data --stdio --children --percent-limit 1.0 \
    > runs/<run-id>/raw/report.inclusive.txt
```

`--no-children` gives self time, `--children` gives inclusive. They are different columns in
the schema — collect both, never blend them.

**Pass B — why the hot function is slow** (answers the counter-based classes). Keep the event
count under the PMU's slots or accept multiplexing error:

```bash
perf stat -e cycles,instructions,branch-misses,cache-references,cache-misses \
          -e L1-dcache-load-misses,LLC-load-misses,LLC-loads \
          -r 5 -o runs/<run-id>/raw/stat.txt -- <run cmd>
```

Then line/instruction attribution on the top function only:

```bash
perf annotate -i runs/<run-id>/raw/perf.data --stdio -s <hot_symbol> \
    > runs/<run-id>/raw/annotate.<hot_symbol>.txt
```

Use precise events (`cycles:pp`) when attributing to specific instructions — sampling skid
otherwise misplaces the cost by several instructions. See the pitfalls section of the notes.

Measure perf's own overhead once (profiled wall time ÷ baseline median) and put it in
`tool.overhead_factor`. If it exceeds ~1.1×, lower `-F` or switch off DWARF unwinding.

## Stage 3 — Normalize

Emit `runs/<run-id>/profile.baseline.json` per `shared/schema/profile.schema.json`, then:

```bash
python3 shared/scripts/validate.py shared/schema/profile.schema.json \
    runs/<run-id>/profile.baseline.json
```

Mapping rules specific to perf:

- `perf report --no-children` overhead % → `hotspots[].pct_of_wall`; multiply by the profiled
  wall time for `self_time_s`. `--children` overhead → `total_time_s`.
- Kind: user functions `cpu-function`; `libc`/BLAS/etc. `library`; `[kernel.kallsyms]` frames
  `runtime`; unresolved `unknown`.
- `samples` per hotspot comes from `perf report --stdio --show-nr-samples`. **A hotspot with
  <100 samples has a share you cannot quote to two digits** — put that in `warnings`.
- Compute `ipc` = instructions ÷ cycles from `perf stat`. Derive
  `hotspots[].amdahl_ceiling` = 1 / (1 − pct_of_wall/100).
- `[unknown]` percentage, multiplexing (`perf stat` prints the counted fraction), and
  non-exclusive nodes all go in `warnings` with magnitudes.

Classification thresholds — cite the actual value in `bottleneck.evidence[]`, and record what
you could **not** measure rather than reasoning past it:

| Observation | Class |
|---|---|
| IPC near core width (≥2.5–3), low miss rates | `compute-bound` |
| Low IPC (<1), high LLC misses, DRAM traffic near STREAM peak | `memory-bandwidth` |
| Low IPC, high LLC misses, DRAM traffic well below peak | `memory-latency` (escalate — perf alone is weak here) |
| High L1/L2 miss rate, LLC absorbs it | `cache` |
| Time flat across many functions, none >5% | `hotspot` = false; look for `serial-fraction` or a systemic cost |
| Scalar `fp_arith_inst_retired` dominating packed | `vectorization` |

## Stage 4 — Attach optimization

Read the hot source. Tie the change to the counter that motivated it, and write the prediction
down **before** measuring:

| Class | Typical levers |
|---|---|
| `memory-bandwidth` | shrink working set, SoA layout, fuse loops, avoid redundant streams, non-temporal stores |
| `cache` | blocking/tiling, padding to kill conflict misses, improve traversal order |
| `vectorization` | remove aliasing (`restrict`), align, unit stride, hoist branches, check the compiler's vectorization report for *why* it refused |
| `compute-bound` | better algorithm/complexity, strength reduction, FMA, fast-math only with an explicit numerics decision |
| `hotspot` in a library | different library or call pattern before rewriting math yourself |

One variable per variant, each as `runs/<run-id>/variants/<name>.patch`. State the Amdahl
ceiling next to the prediction — if the target is 8% of wall time, the ceiling is 1.09× and the
honest move is usually to hand back to the router rather than optimize it.

## Stage 5 — Benchmark

Interleaved A/B is the default on a shared machine:

```bash
python3 shared/scripts/bench.py --label variant-<name> --reps 7 \
    --baseline-command "<baseline run cmd>" \
    --oracle "<correctness check>" --tolerance "<bitwise | rel 1e-12 | ...>" \
    --out runs/<run-id>/bench.<name>.json -- <variant run cmd>
```

Correctness first: a failing oracle kills the variant and its timing is not reported. If
`speedup.significant` is false, the variant did nothing measurable — say exactly that.

## Stage 6 — Reprofile

Re-run stage 2 on the winner, normalize to `profile.variant.json`, and check:

- the targeted symbol's `pct_of_wall` actually fell;
- the counter that motivated the change moved in the predicted direction (miss rate down, IPC
  up, packed-FLOP share up) — if wall time improved but the counter did not, the mechanism
  story is wrong and must not be reported as fact;
- nothing else regressed unexplained; `[unknown]` did not grow.

## Stage 7 — Report

Emit `runs/<run-id>/report.json` per `shared/schema/rewrite-report.schema.json` plus a readable
`report.md`, filling every gate explicitly:

| Gate | Passes when |
|---|---|
| Correctness | Oracle passed on the variant, tolerance stated |
| Significance | Speedup CI excludes 1.0, reps ≥5, interleaved or same-conditions |
| Mechanism | Reprofile shows the target shrinking **and** the motivating counter moving |
| No collateral | No unexplained regression elsewhere |
| Attribution | Every causal claim cites a perf metric actually collected |
| Reproducible | perf version, build flags, host, events, run id all recorded |

Any gate failing → `status: proposed`, not a verified speedup. Close with `limits.cannot_observe`
— for perf that always includes: exact cache behavior (sampled, not simulated), memory-latency
attribution without PEBS/latency events, anything off-CPU that is not in the sampled thread, and
GPU/MPI/I/O entirely.
