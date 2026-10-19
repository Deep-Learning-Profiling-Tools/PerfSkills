# Child registry

Hand-maintained. The root skill reads this to know what exists — a child skill that is not
listed here will not be routed to. Add a row when you add a child; delete the row when you
remove one.

Columns:

- **Skill** — the skill's directory name, which is the name to invoke. Children live in
  `.claude/skills/`; Codex sees the same set through the `.agents/skills` symlink.
- **Tool** — the one profiler this child owns.
- **Answers** — bottleneck classes from `routing.md` it can resolve.
- **Granularity** — finest attribution it produces.
- **Cost** — overhead factor on the profiled run, plus any rebuild/setup burden.
- **Preconditions** — what must be true before dispatching.

## Installed

| Skill | Tool | Answers | Granularity | Cost | Preconditions |
|---|---|---|---|---|---|
| `pw-perf` | Linux `perf` | `hotspot`, `serial-fraction`, `compute-bound`, `memory-bandwidth`, `cache`, `vectorization` (partial) | function, line, instruction | 1.01–1.10× sampling; 1.0–1.2× counters; no rebuild required (`-g` improves attribution) | Linux; `perf` on `PATH`; `perf_event_paranoid <= 2` for user sampling, `<= 0` or `CAP_PERFMON` for most counters; PMU counters usually unavailable in VMs/containers |
| `pw-nsight-systems` | NVIDIA Nsight Systems (`nsys`) | `gpu-idle`, `transfer`, `launch-overhead`, `sync`, GPU-side `hotspot`; MPI/OpenMP/OpenACC context is partial | host+device event timeline by process/thread/stream/device/rank; kernel and transfer summaries | no rebuild; CUDA/NVTX/OSRT without CPU sampling measured 1.0179× on the recorded XSBench B200 run; retain 1.1–2× only as an unmeasured routing budget on other workloads; CPU sampling, verbose providers, metrics, and long traces cost more | `nsys` on `PATH`; NVIDIA driver + supported GPU for CUDA traces; correct reproducible target; provider-specific libraries/permissions. Full seven-stage loop |
| `pw-nsight-compute` | NVIDIA Nsight Compute (`ncu`) | `gpu-occupancy`, `compute-bound`, `memory-bandwidth`, `memory-latency`, `cache`, `instruction-stalls`, `register-pressure`, `warp-divergence`, `gpu-roofline` | one timing-ranked GPU kernel; launch, scheduler, warp-state, instruction, cache/memory, throughput, rules, and SASS counters | local NCU 2025.3 full set advertises 7580 metrics; bounded XSBench large run collected 2374 numeric metrics in 28.979 s and exported in 2.869 s. Scope to one launch and cache by fingerprint; never treat this as representative overhead | `ncu` on `PATH`; supported NVIDIA GPU; hardware-counter permission; exact kernel from a timing profile; reproducible input. Full loop when paired with an absolute timing child |
| `pw-gpa` | GPA (GPU Performance Advisor), via vendored `external/gpa-agent-skill/` (HPCToolkit `hpcrun`→`hpcstruct`→`hpcprof`) | `gpu-occupancy`, `memory-latency`, `sync`, `vectorization` on GPU — instruction-stall attribution via `GINS:LAT_*` codes, with a per-optimizer advice catalog | GPU instruction / source line, per kernel | full rebuild + profiled run per iteration; `nsys` kernel timing on top | NVIDIA GPU + CUDA toolchain; a **built** GPA/HPCToolkit install; `$PW_GPA_ROOT` and `$PW_GPA_TOOLCHAIN` set; benchmark registered in its `assets/benchmarks.json`. `nsys` kernel trace is broken under WSL2 — prefer benchmarks that self-report kernel time |
| `pw-hpctoolkit` | Stock HPCToolkit (`hpcrun`→`hpcstruct`→`hpcprof`), via vendored `external/hpctoolkit-agent-skill/` | GPU-side `hotspot`, `transfer`, `gpu-idle`, sync cost — GPU op time, kernel activity, H2D/D2H copies, sync counts; instruction-level stalls with `gpu=cuda,pc` | calling context → GPU operation; source line with PC sampling + `--gpucfg yes` | one profiled run; `hpcstruct`/`hpcprof` are the slow steps | `hpcrun`/`hpcstruct`/`hpcprof` on `PATH` from a **CUDA-enabled** build (verify — `hpcrun` existing is not proof); NVIDIA GPU; `hatchet`+`pandas` for `read_profile.py`. **Collect-only** — no fix, no benchmark |
| `pw-drgpum` | DrGPUM `memory_liveness`, via vendored `external/drgpum-agent-skill/` | `allocation` — objects resident at peak that need not be, never-freed/never-used/oversized allocations, PyTorch caching-allocator segments. **Bytes, never time** | memory object → allocation site (call path or Python frame) | 0× on the workload — reads a pre-captured profile; analysis is seconds | python3; a DrGPUM `memory_liveness` output dir. Capture needs a built DrGPUM (patched copy at `external/drgpum-agent-skill/profiler-enhanced/`) |
| `pw-gvprof` | GVProf redundancy artifacts, via vendored `external/gvprof-agent-skill/` | GPU value redundancy (spatial/temporal, read/write) — a *cause* hypothesis for `memory-bandwidth` / `cache`, never a cost measurement | `cubin:function:pc_offset` (GPU instruction, **not** source line) | 0× on the workload — reads artifacts captured elsewhere; analysis is seconds | python3; a pre-captured GVProf redundancy dir (4 CSVs + `experiment.xml`). **No live capture** — unvalidated on CUDA 12.x. `pytest` absent here, so upstream's 15-test baseline is unverified |

### Overlapping children — route deliberately

`pw-hpctoolkit` and `pw-gpa` drive the same three binaries. They are not interchangeable:

- **`pw-hpctoolkit` profiles any CUDA executable** with stock HPCToolkit, and only *collects* —
  it produces a database and reads metrics out of it. Use it when calling-context or source
  attribution is required and whenever that question targets an unregistered benchmark; use
  `pw-nsight-systems` first for a host/device timeline question.
- **`pw-gpa` runs the full optimization loop**, but only on a benchmark registered in its
  `benchmarks.json`, and needs a built GPA install with the advisor.

Their event vocabularies differ — a GPA-patched HPCToolkit may expose `gpu=nvidia` where stock
exposes `gpu=cuda`. A toolchain built for one is not automatically usable by the other; confirm
with `hpcrun -L` instead of assuming.

`pw-nsight-systems`, `pw-nsight-compute`, `pw-hpctoolkit`, and `pw-gpa` overlap on GPU evidence but answer different
questions:

- **Use `pw-nsight-systems` by default for host/device timeline questions**: GPU gaps,
  launch/API delay, transfers, synchronization placement, overlap, and NVTX phases. It needs no
  rebuild and can profile any supported CUDA executable.
- **Use `pw-nsight-compute` after the timeline names one expensive kernel** for scheduler/warp
  stalls, registers, occupancy limiters, cache/memory behavior, throughput, and roofline. A full
  set is high-cost, so cache it and never sweep every kernel or every candidate.
- **Use `pw-hpctoolkit` when calling context or source attribution is required**, especially
  when a timeline names the expensive operation but not the responsible caller. It is
  collect-only in this framework.
- **Use `pw-gpa` for instruction/source-line stall diagnosis and its optimization loop** on a
  registered benchmark. Nsight Systems cannot supply occupancy, roofline, cache, register, or
  instruction-stall evidence.

When `pw-gvprof` supplies a redundancy hypothesis, pair it with `pw-nsight-systems` to rank the
affected kernels by measured time before editing.

### Partial children

`pw-gvprof` does not run the full loop. Route to it with open eyes:

- **Enters at stage 3, exits at stage 4.** Cannot capture (stage 2); reprofiling (stage 6)
  requires the *user* to re-capture on their own GVProf install.
- **No time dimension at all.** Redundancy rate is a proxy metric. Ranking targets by it alone
  is the main way to waste effort here — always pair with a timing child
  (`pw-nsight-systems` for kernel durations) and rank by `redundancy × time`.
- **Plan-only by default.** Source mapping is classified `confirmed`/`probable`/`kernel-only`/
  `unmapped`; `unmapped` blocks source edits.
- Consequence for adjudication: the `mechanism` gate can only pass with a second user-supplied
  capture. Without one, its best honest outcome is `status: proposed`.

### Children that optimize something other than time

`pw-drgpum` is the first child whose deliverable is **not a speedup**. It reduces peak device
memory — turning an OOM into a run, or a batch of 8 into 16. Consequences for routing and
adjudication:

- **Do not route time questions to it, and do not accept time answers from it.** DrGPUM records
  no timing counters; any speedup figure attributed to a DrGPUM run is fabricated.
- **Its significance test is a byte threshold** (peak reduction ≥ 2%), not a confidence
  interval. The shared `significance` gate still applies; the evidence is just a different kind.
- **`rewrite-report.schema.json`'s `measurement` block does not fit it** — that block is
  speedup-shaped. A memory result sets it null and reports the peak delta in `diagnosis` and
  `variants[].note`. If a second non-time child ever appears, that is the signal to generalize
  the schema rather than keep working around it.
- Its termination clauses make "nothing further worth doing" a **successful** outcome, reported
  as `status: negative`.

### Children that carry their own method

`pw-gpa` is a thin wrapper over `external/gpa-agent-skill/`, which predates
`templates/child-skill/` and carries its own nine-step loop. Route to it as a full-loop child,
with three things in mind:

- **It is the only child that learns across runs.** A per-benchmark ledger (`pgo-ledger.md`)
  records every KEPT/REVERTED verdict, and a cross-benchmark index keyed by
  *(optimizer, dominant stall reason)* predicts whether a given fix has worked on a similar
  bottleneck before. Do not ask it to re-attempt something its ledger already recorded as
  REVERTED **at the same problem size** — that check is step 3 of its own workflow.
- **Its significance rule is stricter than the shared one** and should be treated as the house
  standard where it applies: a governing metric chosen by precedence (self-reported kernel time
  > nsys > end-to-end), each tagged `[clears 2σ]` or `[NOT SIGNIFICANT]`. End-to-end alone got
  two calls backwards in testing; do not let a report fall back to it silently.
- **Its `mechanism` gate is only partial.** It re-profiles each iteration but does not assert
  that the *targeted finding* shrank, so a KEPT verdict proves the code got faster, not that it
  got faster for the diagnosed reason. Adjudicate accordingly.

It also ships `check_coverage.py`, a literature-coverage aid ("is this benchmark already
covered by GVProf / DrGPUM / GPA?") that is orthogonal to profiling — useful when picking new
benchmarks, not part of any routing decision.

## Candidate children (not installed)

Suggested build order — each covers a column of the taxonomy the installed set cannot reach.
Copy `_child-template/` and fill it in.

| Skill | Tool | Fills the gap |
|---|---|---|
| `pw-rocprof` | AMD `rocprof` / `rocprofv3` | the same two classes on ROCm hardware |
| `pw-vtune` | Intel VTune | `memory-latency`, `sync`, microarchitecture top-down on Intel CPUs |
| `pw-advisor` | Intel Advisor | `vectorization`, roofline with automated loop-level guidance |
| `pw-hpctoolkit` | HPCToolkit | callpath sampling at scale, GPU+CPU unified, low overhead on large runs |
| `pw-tau` | TAU | portable instrumentation across MPI/OpenMP/GPU |
| `pw-scorep-scalasca` | Score-P + Scalasca | `communication`, `load-imbalance`, automated wait-state analysis at rank scale |
| `pw-mpip` | mpiP | lightweight MPI profile — cheap first rung for `communication` |
| `pw-darshan` | Darshan | `io` — per-file, per-rank I/O characterization for HPC jobs |
| `pw-likwid` | LIKWID | pinned counter groups, marker API, bandwidth per memory domain |
| `pw-valgrind` | Cachegrind / Callgrind / Massif | `cache`, `allocation` — simulation, huge overhead, tiny inputs only |
| `pw-gprofng` | gprofng | sampling on systems without `perf` permissions |
| `pw-py-spy` | py-spy / Scalene | Python-driven HPC codes, sampling without rebuild |

## Registry invariants

- **One tool per child.** A child that owns two tools cannot declare one honest cost.
- **No overlap without a reason.** If two children answer the same class at the same
  granularity, the registry row must say which wins by default and why (usually: availability
  or lower cost on this machine).
- **Cost declarations are measured, not guessed.** When a child first runs on a new machine,
  update its row with the observed overhead.
