# Routing: matching questions to profilers

## Bottleneck taxonomy

Every framed question resolves to one of these classes. The class determines the *granularity*
you need, which determines the tool.

| Class | Symptom | Granularity needed | Tool family |
|---|---|---|---|
| `hotspot` | Time concentrated in few functions | function / line | sampling profiler |
| `compute-bound` | High IPC, near peak FLOP/s | loop nest + counters | counter/roofline tool |
| `memory-bandwidth` | Low IPC, high BW, low AI | loop nest + counters | counter/roofline tool |
| `memory-latency` | Low IPC, low BW, high stall cycles | instruction / access site | counter + memory profiler |
| `cache` | Miss rate at a level dominates | data structure + access pattern | cache simulator / counters |
| `vectorization` | Scalar ops where SIMD expected | loop nest + compiler report | compiler report + counters |
| `sync` | Threads idle at barriers/locks | thread timeline | threading/timeline tool |
| `load-imbalance` | Rank/thread time spread wide | per-rank aggregate | MPI/OpenMP profiler |
| `communication` | Time in MPI calls, wait vs. transfer | per-call, per-pair | MPI trace/profiler |
| `io` | Time in read/write/metadata | per-file, per-syscall | I/O profiler / strace-class |
| `gpu-occupancy` | Kernels resident but slow | kernel + launch config | GPU kernel profiler |
| `instruction-stalls` | Warps are active but cannot issue | kernel + warp-state counters | GPU kernel profiler |
| `register-pressure` | Register footprint limits residency or spills | kernel + launch/compiler counters | GPU kernel profiler + compiler report |
| `warp-divergence` | Lanes under-participate on control flow | kernel + branch/thread counters | GPU kernel profiler |
| `gpu-roofline` | Need compute-vs-memory ceiling evidence | kernel + arithmetic/traffic counters | GPU roofline tool |
| `gpu-idle` | GPU has gaps | host+device timeline | GPU timeline tracer |
| `transfer` | H2D/D2H dominates | copy events + sizes | GPU timeline tracer |
| `launch-overhead` | Many tiny kernels/calls | API trace | GPU/API tracer |
| `allocation` | Time or footprint in malloc/free | call site | heap profiler |
| `serial-fraction` | Speedup plateaus with resources | scaling curve | scaling study + sampler |

Note the asymmetry: *timeline* tools tell you **when and where** things stall; *counter* tools
tell you **why** an instruction stream is slow. Do not ask one to do the other's job.

## Capacity questions are not time questions

Not every performance question is about speed. "It won't fit" — CUDA OOM, a batch size capped
by device memory, a job that needs a bigger node — is a **capacity** question, and framing it
as a time question sends it to the wrong tool and produces the wrong deliverable.

| User says | Framed as |
|---|---|
| "it OOMs" / "I want a bigger batch" | What is peak device memory, and which resident objects do not need to be? |
| "it uses too much RAM" | Peak footprint, and the allocation sites that own it |

These route to `allocation` and are answered in **bytes**, with a peak-memory reduction as the
result. Do not ask a memory profiler for a speedup, and do not report one; a memory tool that
records no timing counters cannot produce a time number that means anything. The two axes can
interact — a smaller footprint may improve locality, or spilling to host memory may dominate
runtime — but that is a claim requiring a *timing* tool to establish, not an assumption.

## Cost model

Rank candidates by total cost, not overhead alone:

```
cost = overhead_factor × baseline_runtime × runs_needed
     + rebuild_cost        # recompile with -g / instrumentation / different toolchain
     + setup_cost          # root/perf_event_paranoid, driver counters, license, module load
     + analysis_cost       # trace size, post-processing time, human interpretation
```

Rough overhead bands — always confirm against the child's own declaration:

| Mechanism | Overhead | Notes |
|---|---|---|
| Timer/wall-clock harness | ~1.00× | always affordable, near-zero insight |
| Statistical sampling | 1.01–1.10× | first stop for `hotspot` |
| Hardware counters (multiplexed) | 1.0–1.2× | needs permissions; multiplexing adds error |
| Callpath sampling + unwinding | 1.1–1.5× | needs frame pointers or good DWARF |
| GPU timeline trace | 1.1–2× | trace size grows fast |
| MPI interposition | 1.05–1.5× | trace mode much worse than profile mode |
| GPU kernel replay w/ full counters | 10–100× per kernel | scope to *one* kernel, never whole run |
| Binary instrumentation / simulation | 10–200× | shrink the input first |

**Shrink the input before escalating.** A 100× tool on a 1% input is cheaper than a 1.1× tool on
the full run — but only if the small input preserves the bottleneck. Verify that it does
(same hotspot ranking, same bottleneck class) before trusting results from it.

## Escalation ladder

Move down only when the current rung cannot answer the framed question. Each rung hands the
next one a *narrowed scope*.

1. **Time it.** Establish a reproducible baseline with variance. If run-to-run noise exceeds
   the effect you are chasing, stop and fix the measurement before profiling anything.
2. **Sample it.** Where does time go, at function granularity? Resolves `hotspot`,
   `serial-fraction`, and usually distinguishes CPU-side from GPU-side or I/O.
3. **Attribute it.** Callpath + line-level attribution on the hot region only.
4. **Explain it.** Counters/roofline/timeline on the narrowed region: which class of stall.
5. **Simulate it.** Cache/dependence simulation, kernel replay. Last resort, smallest scope.

For NVIDIA GPU optimization, use a staged form of rungs 4–5: Nsight Systems identifies and
times the expensive kernel; Nsight Compute replays one launch to explain it. Keep NCU's full
metrics and report off-context. The agent receives only a bounded diagnostic capsule with raw
metric provenance and the named source slice. Use timeline profiling for every correct
candidate; repeat full NCU only when a candidate changes the diagnosed mechanism and for the
accepted winner's fresh confirmation.

Skip rungs only with a stated reason (e.g. user already has a sampling profile with a run id).

## Preconditions checklist

Check before dispatch, not after a failed run:

- Tool on `PATH` and version known (`--version`).
- Permissions: `/proc/sys/kernel/perf_event_paranoid`, `perf_event_open` in container,
  GPU counter access flag, root/`CAP_PERFMON`.
- Symbols: built with `-g`; `-fno-omit-frame-pointer` if unwinding by frame pointer.
- The run is reproducible: fixed seed, fixed input, pinned threads, known node.
- The correctness oracle can actually be evaluated from the run's output.
- Exclusive access, or noise characterized. On a shared node, treat all deltas below the
  measured noise floor as zero.

## Disqualifiers

Route *away* from a child when:

- Its overhead × runtime exceeds the user's stated budget.
- It requires a rebuild the user cannot do (production binary, vendor library).
- It measures a mechanism unrelated to the framed class (do not open a cache simulator for a
  load-imbalance question).
- A cheaper child already answered the question and only the *fix* remains — go straight to
  the child that owns the relevant optimization, or hand the rewrite back to the same child.
