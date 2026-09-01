---
name: pw-gvprof
description: Analyze GVProf GPU value-redundancy artifacts (spatial/temporal read/write CSVs) into a normalized ProfileWeaver profile, and propose CUDA optimizations backed by that evidence. Use when someone asks about redundant loads/stores, duplicate values across lanes or over time, wasted memory traffic in CUDA kernels, or has an existing GVProf/redundancy database to interpret. Consumes ALREADY-CAPTURED artifacts - it cannot run live capture, and redundancy rate is not a time measurement, so it must be paired with a timing profiler before any target is called worth optimizing.
---

# pw-gvprof — GVProf redundancy analysis

Wrapper around the vendored **[gvprof-agent-skill](../../../external/gvprof-agent-skill/)**
repo (`external/gvprof-agent-skill/`), which owns the parsing, ranking, and planning logic.
This skill adapts it to the ProfileWeaver contract: normalize to the shared profile schema,
and report against the trust gates.

**This is a partial child.** It enters the loop at stage 3 and exits at stage 4. Read
"Capability boundary" before promising anything.

## Declared capability

- **Answers**: value redundancy in CUDA kernels — spatial (across lanes in one access) and
  temporal (same location, repeated value) read/write redundancy, ranked per PC offset
- **Cannot answer**: anything about **time**. No wall time, no kernel duration, no occupancy,
  no bandwidth, no Amdahl ceiling. Also no live capture, no CPU-side cost, no MPI, no I/O
- **Granularity**: `cubin_id : function_index : pc_offset` — GPU instruction offsets, *not*
  source lines. Source attribution is a separate, uncertain step (see below)
- **Overhead**: 0× on the workload — it reads artifacts someone else captured. Analysis is
  seconds of CPU
- **Requires**: python3; the vendored repo; a pre-captured GVProf redundancy directory
  (`spatial_read_t0.csv`, `spatial_write_t0.csv`, `temporal_read_t0.csv`,
  `temporal_write_t0.csv`, `experiment.xml`)
- **Must be paired with**: a timing child (`pw-nsight-systems` for kernel durations,
  `pw-nsight-compute` for per-kernel counters) to supply the time dimension

## Capability boundary — read before dispatching

Three hard limits. Each is upstream's own documented position, not caution I invented:

1. **No live capture.** The `gpu=nvidia,redundancy` Compute Sanitizer callback path is
   *not validated* on CUDA 12.x in this repo. The user captures artifacts themselves on a
   working GVProf install and hands you the directory. If asked to capture, say so plainly and
   hand back to the router — do not attempt it and do not report a partial run as a capture.
2. **Redundancy is a proxy, not a cost.** A kernel with 66% spatial read redundancy that
   accounts for 0.3% of wall time is worth nothing. **Never rank optimization targets by
   redundancy rate alone.** Get kernel durations from a timing child first, then rank by
   `redundancy × time`. This is the single most likely way to waste the user's effort here.
3. **Source mapping is uncertain.** GVProf gives `cubin:ID:function:IDX:pc:OFFSET`. The
   optimizer classifies mapping as `confirmed` / `probable` / `kernel-only` / `unmapped`.
   **`unmapped` is blocked — never edit source from it.** `probable` means propose and say so.

Upstream is **plan-only** by default: it does not edit source, and applying a change is a
separate explicit request, one candidate at a time, followed by rebuild + correctness check.

**Do not modify anything under `external/gvprof-agent-skill/`.** It is a separate git repo
(`Honrrii/gvprof-agent-skill`, branch `feature/redundancy-codec-v0`), and its
`gvprof-redundancy-analyzer` skill declares itself the authority for profile parsing. Changes
belong upstream, not in a local edit that the next pull silently reverts.

## Stage 1 — Preflight

```bash
cd external/gvprof-agent-skill
ls <profile-dir>          # expect the 4 CSVs + experiment.xml
PYTHONPATH=src python3 -m pytest -q      # upstream baseline: 15 passing
```

- `pytest` is **not installed** for the system python3 here. Either `pip install pytest` into a
  venv or skip the regression check and say you skipped it — do not report the 15-test baseline
  as verified when you did not run it.
- If the supplied path has a `redundancy/` child, use that child as the parser input.
- **Get the time source now, not later.** Ask for kernel durations, or hand back to the router
  for a timing child. Everything downstream is unrankable without it.
- Never modify the original artifacts. Copy them into `runs/<run-id>/raw/` if you need to.

## Stage 2 — Profile — NOT AVAILABLE

Capture is the user's responsibility on their own GVProf install. Record in
`preflight.json` how the artifacts were captured (tool version, GPU, CUDA version, workload,
input) — if that provenance is unknown, every conclusion inherits the uncertainty and the
`reproducible` gate fails. Say that rather than quietly assuming.

## Stage 3 — Normalize

Run upstream's parser, then translate its ProfileIR into the ProfileWeaver schema:

```bash
cd external/gvprof-agent-skill
PYTHONPATH=src python3 -m gvprof_skill.cli <redundancy-csv-dir> \
    --out <abs-path>/runs/<run-id>/raw/profile_ir.json --summary

# rank several captures against each other
PYTHONPATH=src python3 -m gvprof_skill.cli --survey data/ --survey-out <path>.md
```

`--out` is a **file** path, not a directory.

Then write `runs/<run-id>/profile.baseline.json` and validate:

```bash
python3 shared/scripts/validate.py shared/schema/profile.schema.json \
    runs/<run-id>/profile.baseline.json
```

Mapping ProfileIR → schema:

| ProfileIR | Schema | Rule |
|---|---|---|
| `hotspots[].location` | `hotspots[].name` | keep the `cubin:…:pc:…` form verbatim |
| — | `hotspots[].kind` | `gpu-kernel` |
| `hotspots[].kind` (`spatial_read` …) | `hotspots[].metrics.redundancy_kind` | |
| `hotspots[].redundancy_rate` | `hotspots[].metrics.redundancy_rate` | |
| `redundant_operations` / `total_operations` | `hotspots[].metrics.*` | |
| `details.pc_offset`, `cubin_id`, `function_index` | `hotspots[].metrics.*` | keep for re-mapping |
| — | `hotspots[].file` / `line` | **null** unless mapping is `confirmed` |
| paired timing child | `hotspots[].self_time_s`, `pct_of_wall`, `totals.wall_time_s` | the only legitimate source |

Rules that keep this honest:

- **Redundancy rate never goes in a time field.** If no timing child has run,
  `totals.wall_time_s` has no honest value — do not fabricate one to satisfy the schema.
  Emit the profile with a `warnings` entry of kind `no-time-dimension` stating that hotspot
  ranking is by proxy metric only, and mark `bottleneck.confidence` as `low`.
- `bottleneck.class` is `memory-bandwidth` when redundancy implies wasted traffic, or `cache`
  when temporal redundancy implies re-fetching resident data — but only with time evidence to
  support it. Without time, use `indeterminate` and list both under `alternatives` with
  `discriminating_measurement` naming the timing child.
- Put GVProf's own quirks in `warnings`: `data_type: UNKNOWN`, the upstream `t_pc_offest`
  column typo, hotspot lists truncated to top 20, and single-thread (`_t0`) artifacts that say
  nothing about other threads.

## Stage 4 — Attach optimization

Use upstream's planner, which inspects sources, kernels, launch sites, build system, and git
state, and classifies mapping confidence:

```bash
cd external/gvprof-agent-skill
PYTHONPATH=src python3 -m gvprof_skill.optimizer_cli \
    --project-root /path/to/cuda/project \
    --profile-dir /path/to/gvprof/profile \
    --out <abs-path>/runs/<run-id>/variants/plan.json
```

Levers by redundancy kind:

| Finding | Typical lever |
|---|---|
| spatial read | neighboring lanes loading the same value — stage through shared memory, broadcast from one lane, restructure indexing to unit stride |
| spatial write | lanes writing identical values — have one lane write, or fold into a reduction |
| temporal read | same location re-read with an unchanged value — hoist into a register, cache in shared memory across the loop |
| temporal write | same value written repeatedly — guard the store, or move it out of the loop |

Write the predicted speedup **and** the Amdahl ceiling from the paired timing data before
measuring. If the ceiling is unknown because no timing child ran, the honest prediction is
"unknown" — record that, and treat a later "it got faster" as unexplained until reprofiled.

## Stage 5 — Benchmark

Standard ProfileWeaver benchmarking — this part is fully available whenever the user can build
and run the CUDA project:

```bash
python3 shared/scripts/bench.py --label variant-<name> --reps 7 \
    --baseline-command "<baseline run>" --oracle "<correctness check>" \
    --tolerance "<bitwise | rel 1e-6>" \
    --out runs/<run-id>/bench.<name>.json -- <variant run>
```

GPU-specific care: warm up the device (first-launch and JIT costs land in rep 1 — that's what
`--warmup` is for), pin clocks if permitted (`nvidia-smi -lgc`) or report the variance, and
make the oracle tolerance explicit since reordering GPU floating-point reductions legitimately
changes the last bits.

## Stage 6 — Reprofile — REQUIRES A NEW CAPTURE

Confirming the mechanism means re-capturing GVProf artifacts on the variant, which only the
user can do (see stage 2). Hand back a precise request: same workload, same input, same GVProf
version, capture into a new directory. Then re-run stage 3 and compare redundancy rates at the
targeted PC offsets.

If the user cannot re-capture, the **mechanism gate fails** — report `status: proposed` with a
measured speedup and an unconfirmed mechanism. That is a legitimate, useful result; a claim of
a verified redundancy fix without the second capture is not.

## Stage 7 — Report

Emit `runs/<run-id>/report.json` per `shared/schema/rewrite-report.schema.json`.

| Gate | Passes when |
|---|---|
| Correctness | oracle passed on the variant, tolerance stated (GPU FP reordering acknowledged) |
| Significance | speedup CI excludes 1.0, reps ≥5, warmup discarded |
| Mechanism | **re-captured** GVProf profile shows redundancy falling at the targeted PC offsets |
| No collateral | no other kernel regressed; total wall time improved, not just the one kernel |
| Attribution | every claim cites parsed redundancy evidence, with mapping confidence stated |
| Reproducible | GVProf version, capture provenance, CUDA version, GPU, commit, run id recorded |

`limits.cannot_observe` for this child always includes: wall time and kernel duration,
occupancy, achieved bandwidth, anything CPU-side, and any thread beyond the captured `_t0`.
Add `mapping_confidence` to `limits.assumptions` whenever it is below `confirmed`.

See `references/tool-notes.md` for artifact format, the survey scoring formula, and pitfalls.
