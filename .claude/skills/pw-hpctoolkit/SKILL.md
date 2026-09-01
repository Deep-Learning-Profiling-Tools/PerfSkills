---
name: pw-hpctoolkit
description: Profile an arbitrary CUDA or GPU application with stock HPCToolkit - hpcrun to collect, hpcstruct to recover program structure, hpcprof to build a database - then read GPU time, kernel activity, host-device copy and synchronization metrics out of it. Use when asked to profile, benchmark, or collect performance metrics for a CUDA executable with HPCToolkit, or when a GPU workload needs a first-pass profile and no tool has been chosen yet.
allowed-tools: Read, Edit, Bash, Grep, Glob
---

# pw-hpctoolkit — HPCToolkit GPU profiling

Wrapper around the vendored **[hpctoolkit-agent-skill](../../../external/hpctoolkit-agent-skill/)**
(`external/hpctoolkit-agent-skill/`), which owns the collection workflow. This skill adds the
ProfileWeaver-facing contract.

**Read
[external/hpctoolkit-agent-skill/SKILL.md](../../../external/hpctoolkit-agent-skill/SKILL.md)
and follow its workflow.** Everything below is the frame around it.

## What this child is for

It **collects**. It runs `hpcrun` → `hpcstruct` → `hpcprof` on any CUDA executable and produces
a database, then reads GPU metrics out of it. It does not propose an optimization, benchmark a
variant, or verify a rewrite — so on its own it answers *where the time goes*, not *what to
change*.

That makes it the natural **first rung** for a GPU workload (routing.md's ladder, step 2–3):
cheap, general, and it narrows the scope for a more expensive child.

## Declared capability

- **Answers**: `hotspot` and `gpu-idle`/`transfer`-shaped questions on the GPU side — total GPU
  operation time, kernel activity, explicit copy time, H2D and D2H transfers, synchronization
  time and counts, launch behavior, occupancy info *when the database carries it*. With
  `gpu=cuda,pc`, instruction-level GPU stall attribution
- **Cannot answer**: anything it did not collect. It proposes no fixes, measures no variant, and
  has no ledger. Occupancy and roofline math belong to `pw-nsight-compute`
- **Granularity**: calling context → GPU operation; source line with PC sampling and
  `hpcstruct --gpucfg yes`
- **Cost**: one profiled run (`hpcrun` sampling is cheap) plus `hpcstruct`/`hpcprof`, which are
  the slow steps on large binaries
- **Requires**: `hpcrun`, `hpcstruct`, `hpcprof` on `PATH`, from a **CUDA-enabled** HPCToolkit
  build; an NVIDIA GPU; a CUDA executable that already runs correctly. `read_profile.py` needs
  `hatchet` and `pandas`

## Preconditions — check, do not assume

The upstream workflow is emphatic about this and it is the main way this child wastes a run:

1. **`hpcrun` existing does not mean CUDA support exists.** A non-CUDA build fails at
   `-e gpu=cuda`. Check before profiling, and never report GPU profiling as succeeded when the
   build lacks CUDA.
2. **Never invent events.** `hpcrun -L` is the authority for what this machine supports;
   `hpcrun -L | grep -i -E 'gpu|cuda'` for the GPU ones. Event names differ between builds and
   forks — a patched HPCToolkit may expose `gpu=nvidia` rather than `gpu=cuda`, so a
   toolchain built for another tool is not automatically usable here.
3. **PC sampling is not always available.** Try `gpu=cuda,pc` only when instruction-level detail
   is actually requested; on an unsupported-feature error, fall back to `gpu=cuda` and *say* the
   fine-grained pass was unavailable.
4. **Run the executable un-profiled first.** A program that already fails must not be profiled —
   otherwise an application bug gets reported as a profiling failure.

At merge time on this machine: CUDA 12.8 and an RTX 3090 are present, `hatchet` is **not**
installed, and no HPCToolkit was on `PATH`. A build exists under the GPA install
(`$PW_GPA_ROOT/gpa/hpctoolkit/bin`), but it is the GPA-patched fork — verify its event names
with `hpcrun -L` before assuming `gpu=cuda` works, rather than treating it as stock HPCToolkit.

## Relationship to `pw-gpa`

Both drive `hpcrun` → `hpcstruct` → `hpcprof`, so route deliberately:

| | `pw-hpctoolkit` | `pw-gpa` |
|---|---|---|
| Target | any CUDA executable | only benchmarks registered in `benchmarks.json` |
| Needs | stock CUDA-enabled HPCToolkit | a built GPA install + advisor |
| Produces | a database and metrics | ranked advice, an applied fix, a KEPT/REVERTED verdict |
| Loop | collect and report | the full nine-step optimization loop |

Prefer `pw-hpctoolkit` for an arbitrary or unregistered workload and for first-pass collection;
prefer `pw-gpa` when the benchmark is registered and the goal is an optimization, not a profile.

## ProfileWeaver contract

This child covers stages 1–3 of `templates/child-skill/` (preflight, profile, normalize) and
stops. It **cannot reach a `verified` rewrite on its own** — there is no variant, no benchmark,
and no reprofile — so its honest report status is `negative` (nothing worth acting on) or a
profile handed onward, never `verified`.

| Gate | Status |
|---|---|
| Correctness | n/a — nothing is changed |
| Significance | n/a — no variant is measured |
| Mechanism | n/a |
| No collateral | n/a |
| Attribution | **applies** — every metric quoted must come from the database this run produced, named as it appears there |
| Reproducible | **applies** — record the exact `hpcrun`/`hpcstruct`/`hpcprof` commands, events used, measurement and database directories, HPCToolkit version, and whether the run was coarse or PC-sampled |

Normalize into `shared/schema/profile.schema.json` when handing off: GPU operations become
`hotspots[]` with `kind: gpu-kernel` (or `memcpy` for copies), copy and sync metrics go in
`metrics{}`, and `totals.gpu_busy_s` comes from total GPU operation time. Put anything the
database did *not* contain in `warnings` — the upstream skill's rule that not every metric
appears in every database is exactly the schema's "a null is information, a guess is
contamination".

`limits.cannot_observe`: whatever the chosen events did not collect. A coarse `gpu=cuda` run
says nothing about instruction-level stalls; state that rather than implying full coverage.

## Repairs on merge

The vendored directory arrived with PDF-extraction damage — `SKILL.md`'s frontmatter and code
blocks were flattened onto single lines (the skill could not load, and multi-command blocks read
as one command), and the `Makefile` used spaces instead of tabs. Both are fixed; see
[PROVENANCE.md](../../../external/hpctoolkit-agent-skill/PROVENANCE.md). There is no upstream
git here, so ProfileWeaver tracks these files directly — edits are edits to ProfileWeaver.
