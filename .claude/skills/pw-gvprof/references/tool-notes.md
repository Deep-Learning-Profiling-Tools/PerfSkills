# GVProf — mechanics and pitfalls

Loaded at stage 3. Upstream logic lives in `external/gvprof-agent-skill/` and is authoritative;
this file records how to drive it and where it can mislead.

## Versions

| Component | Where | Status |
|---|---|---|
| `gvprof-agent-skill` | `external/gvprof-agent-skill/`, repo `Honrrii/gvprof-agent-skill`, branch `feature/redundancy-codec-v0`, at `28dd951` | CLI verified working on the bundled `data/vectorAdd_baseline` sample |
| `pytest` | system python3 | **not installed** — the 15-test upstream baseline was not run here |
| GVProf itself | not installed on this machine | capture happens on the user's machine |

## Artifact format

A redundancy directory holds four CSVs plus `experiment.xml`:

```
spatial_read_t0.csv   spatial_write_t0.csv
temporal_read_t0.csv  temporal_write_t0.csv   experiment.xml
```

Each CSV is **sectioned**, not a flat table — the parser handles this, but read one before
interpreting output:

```
kernel_id,231                                        <- section marker
redundant_access_count,total_access_count,redundancy_rate
2,3,0.666667                                         <- SUMMARY for this section
memory_op_id,cubin_id,function_index,pc_offset,value,data_type,vector_size,unit_size,count,rate,norm_rate
9223372036854775810,1858620682,0,176,,UNKNOWN,v:32,u:32,1,1,0.333333   <- PER-PC rows
...
cpu_thread,0                                         <- a second section
```

Section markers are `kernel_id` and `cpu_thread`. The `_t0` suffix means **thread 0 only** —
these artifacts say nothing about any other CPU thread.

## Commands

```bash
cd external/gvprof-agent-skill

# Parse one profile to ProfileIR JSON (--out is a FILE path)
PYTHONPATH=src python3 -m gvprof_skill.cli <csv-dir> --out <file>.json --summary

# Rank several captures against each other
PYTHONPATH=src python3 -m gvprof_skill.cli --survey <data-root> --survey-out <file>.md

# Plan-only optimization against a real CUDA project
PYTHONPATH=src python3 -m gvprof_skill.optimizer_cli \
    --project-root <cuda-project> --profile-dir <csv-dir> --out <plan>.json

# Upstream regression baseline (15 tests) - needs pytest installed
PYTHONPATH=src python3 -m pytest -q
```

If the path handed to you contains a `redundancy/` child directory, pass that child.

## Metric dictionary

| ProfileIR field | Meaning | Schema destination |
|---|---|---|
| `metrics[].redundancy_rate` | section-level rate, e.g. 2/3 accesses redundant | `bottleneck.evidence[]` |
| `hotspots[].redundancy_rate` | per-PC rate — `norm_rate`, falling back to `rate` when `norm_rate` is 0 | `hotspots[].metrics.redundancy_rate` |
| `hotspots[].location` | `cubin:<id>:function:<idx>:pc:<offset>` | `hotspots[].name` |
| `hotspots[].kind` | `spatial_read` \| `spatial_write` \| `temporal_read` \| `temporal_write` | `hotspots[].metrics.redundancy_kind` |
| `details.pc_offset` / `cubin_id` / `function_index` | GPU instruction identity | `hotspots[].metrics.*` — keep, they are the only handle for re-mapping |
| `details.value`, `data_type` | the redundant value and its type | often empty / `UNKNOWN` |
| `diagnosis[]`, `optimization_hints[]` | rule-based text | starting points for stage 4, **not** evidence |

Survey scoring (`survey.py`), for cross-capture ranking:

```
impact_score = redundancy_rate × log10(redundant_operations + 1)
priority: >= 0.30 high | >= 0.10 medium | > 0 low | otherwise none
```

This score has **no time term**. It ranks redundancy against redundancy, never against cost.

## Pitfalls

- **Two different rates, both called `redundancy_rate`.** The section summary (e.g. 66.7% =
  2 of 3 accesses) and the per-PC `norm_rate` (e.g. 33.3%) are different denominators. In the
  bundled `vectorAdd_baseline` both appear in the same report. State which one you mean, every
  time.
- **Redundancy ≠ time.** The central trap. High redundancy in a kernel that costs 0.3% of wall
  time is worth nothing. Rank by `redundancy × kernel_time`, with the time from a timing child.
- **Hotspots are truncated to the top 20** per profile (`parser.py`). A finding "not in the
  list" was not necessarily absent. Note the truncation in `warnings`.
- **`t_pc_offest`** — a typo in GVProf's own upstream column header for temporal files. The
  parser mirrors it into `t_pc_offset`; both keys may appear. Do not "fix" it in the CSVs.
- **PC offsets are not source lines.** Mapping to source is a separate step with its own
  confidence (`confirmed` / `probable` / `kernel-only` / `unmapped`). `unmapped` blocks source
  edits. See `docs/research/gvprof_source_mapping_v1_audit.md` and
  `docs/research/artifacts/pathfinder_source_mapping_audit.json`.
- **`data_type: UNKNOWN`** is common — the redundant value's type is often unrecoverable, which
  limits how specific an optimization proposal can honestly be.
- **`_t0` only.** Single CPU thread. Multi-threaded launch behavior is unobserved.
- **Live capture is unvalidated on CUDA 12.x.** The `gpu=nvidia,redundancy` Compute Sanitizer
  path is distinct from the CUPTI lifecycle repair that *was* validated. Do not conflate them.
  Background: `docs/research/gvprof_cuda12_live_pipeline_bridge_design.md`,
  `docs/research/cupti_cuda12_teardown_order_patch_results.md`. Load these only when the request
  is actually about live capture.
- **Stale `profile_ir.json` at the repo root** has `"event": "redundancy"` from an older ad hoc
  run; today's CLI writes `event = <dir name>`. Regenerate rather than trusting checked-in IR.
- **Capture provenance is often missing.** If you cannot state GVProf version, GPU, CUDA
  version, and the exact workload behind a CSV, the `reproducible` gate fails — say so.

## Bundled sample data

`data/{vectorAdd_baseline, vectorAdd_f64, pathfinder_baseline, reduction_baseline}` are real
GVProf outputs, useful as fixtures and for checking the toolchain end to end.
`pathfinder_baseline` is upstream's highest-priority survey target
(`impact_score = 0.334`, driven by `temporal_read = 5.1%`).

Smoke test — should print a 66.7% spatial_read summary and three hotspots:

```bash
cd external/gvprof-agent-skill
PYTHONPATH=src python3 -m gvprof_skill.cli data/vectorAdd_baseline --out /tmp/gv.json --summary
```
