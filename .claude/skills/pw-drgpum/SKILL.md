---
name: pw-drgpum
description: Find, quantify, and fix GPU *memory* inefficiencies in CUDA and PyTorch applications from DrGPUM memory_liveness profiles - which bytes are resident on the device without earning their place. Use when peak GPU memory is the constraint: CUDA out-of-memory, batch size capped by memory, a DrGPUM output directory that needs interpreting, or verifying a memory fix. Not for kernel timing, occupancy, or bandwidth - DrGPUM records no timing counters at all.
allowed-tools: Read, Edit, Bash, Grep, Glob
---

# pw-drgpum — DrGPUM (GPU memory)

Wrapper around the vendored **[drgpum-agent-skill](../../../external/drgpum-agent-skill/)**
(`external/drgpum-agent-skill/`), which owns the analysis pipeline, the detectors, and the
ledger. This skill adds the ProfileWeaver-facing contract.

**Read
[external/drgpum-agent-skill/.claude/skills/drgpum-memory-pgo/SKILL.md](../../../external/drgpum-agent-skill/.claude/skills/drgpum-memory-pgo/SKILL.md)
and follow its workflow.** It is the orchestrator; everything below is the frame around it.

## The one thing to get right

DrGPUM measures **bytes, not time**. It records no kernel timing, no SM occupancy, no
bandwidth. A speedup number sourced from a DrGPUM run is fabricated — the upstream skill lists
that as a red flag, and so does this one. If the question is about how fast something runs,
this is the wrong child: hand back to the router for `pw-nsight-compute` or
`pw-nsight-systems`.

The deliverable here is a **peak-memory reduction**, which is a real optimization outcome in
its own right — it is what turns an OOM into a run, or a batch size of 8 into 16.

## Declared capability

- **Answers**: `allocation` — objects resident at peak that need not be, never-freed objects,
  never-used objects, oversized allocations, and PyTorch caching-allocator segment behavior
- **Cannot answer**: anything time-shaped (`gpu-occupancy`, `memory-bandwidth`, `transfer`,
  `launch-overhead`, `hotspot`), and any question with no profile in hand
- **Granularity**: memory object → allocation site, via `memory_liveness.csv.context` call
  paths or `torch_python_states.txt` Python frames
- **Cost**: 0× on the workload — it reads a profile someone else captured. Analysis is seconds
- **Requires**: python3; a DrGPUM `memory_liveness` output directory. Capture itself needs a
  built DrGPUM — the vendored tree carries none (pruned), so use an install such as
  `~/Projects/DrGPUM/DrGPUM` or `~/Projects/new_DrGPUM/DrGPUM`
- **Escalates to**: `pw-nsight-compute` / `pw-nsight-systems` for anything timing-shaped

## Pipeline

The upstream repo ships four skills: an orchestrator plus three stages it delegates to.

| Skill | Role |
|---|---|
| `drgpum-memory-pgo` | orchestrator — the workflow to follow |
| `drg-profile-analyzer` | stage 1: parse and rank findings |
| `optimization-suggester` | stage 2: turn findings into source changes |
| `benchmark-validator` | stage 3: compare before/after profiles, decide iterate-or-stop |

They live under `external/drgpum-agent-skill/.claude/skills/`, which is **not** one of the paths
either agent scans from this repo's root — Claude Code reads the root `.claude/skills/`, Codex
reads `.agents/skills/` walking up from the working directory. So they are documentation this
wrapper points into, not separately routable children. That is deliberate: one tool, one child.

(Running an agent with its working directory *inside* `external/drgpum-agent-skill/` is the one
case where they could register on their own. Work from the ProfileWeaver root instead.)

Run the scripts **from `external/drgpum-agent-skill/`**:

```bash
cd external/drgpum-agent-skill
python3 .claude/skills/drgpum-memory-pgo/scripts/analyze_profile.py <profile_dir> --json findings.json
python3 .claude/skills/drgpum-memory-pgo/scripts/compare_profiles.py <before_dir> <after_dir>
python3 .claude/skills/drgpum-memory-pgo/scripts/show_ledger.py
```

`main.py` is the single entry point for the same operations
(`profile` / `analyze` / `agent` / `compare` / `ledger`); only `profile` needs a GPU.

**The vendored tree is pruned to the core skill** — 320K, holding `.claude/skills/`, the
`agent/` package, and the docs. Upstream's DrGPUM source, test fixtures, captured profiles,
benchmark suite, and git history were removed; `PROVENANCE.md` records the upstream URL and
commit so any of it can be recovered by re-cloning. Because there is no local `.git`,
ProfileWeaver tracks these files directly — treat edits here as edits to ProfileWeaver, and
port anything worth keeping back upstream deliberately.

One fix was applied during the merge and is marked in place: the three scripts' `sys.path`
offset was `parents[3]`, which pointed at `.claude/` rather than the repo root after upstream
moved `skills/` under `.claude/`; it is now `parents[4]`. Without it every script died with
`ModuleNotFoundError: No module named 'agent'`.

## ProfileWeaver contract

Map onto the shared gates (`shared/schema/rewrite-report.schema.json`):

| Gate | Satisfied by | Notes |
|---|---|---|
| Correctness | the orchestrator's step 5 — tests, or output tensors within tolerance, **before** any number is reported | upstream is emphatic about the ordering; keep it |
| Significance | peak reduction ≥ 2% (termination clause 1), measured identically both times | the noise floor here is a *byte* threshold, not a CI |
| Mechanism | the re-profile shows the **targeted object** gone or smaller, not merely a lower peak | one finding per iteration exists precisely to keep this attributable |
| No collateral | peak measured on the same batch size and input both times | comparing profiles at different batch sizes is a listed red flag |
| Attribution | the object was located by one of step 3's methods, and which one is stated | "unlocated" is a valid, reportable outcome — never name a tensor you did not locate |
| Reproducible | ledger row: finding kind, object id, action, peak before/after, correctness | append-only; rejected attempts stay in it |

**Schema fit — read before filling out a report.** `rewrite-report.schema.json`'s `measurement`
block is speedup-shaped (`baseline_median_s`, `speedup`, `speedup_ci95`). None of that applies
to a memory result. Report the peak-memory delta in `variants[].note` and `diagnosis`, set
`measurement` to null, and say plainly in `limits.assumptions` that the improvement metric is
peak device bytes, not time. Do **not** synthesize a speedup to fill the fields — that is
exactly the fabrication both this skill and the schema's `attribution` gate exist to prevent.

`limits.cannot_observe` for this child always includes: kernel time, occupancy, bandwidth,
transfer cost, and — when the profile lacks ACCESS operations or sub-allocation data — usage-
based findings and PyTorch tensor-level attribution. The analyzer prints those caveats itself;
carry them into the report rather than dropping them.

## Termination is success

The orchestrator lists six termination clauses (peak reduction < 2%, within 10% of optimal,
three iterations with no accepted fix, correctness failed twice on one finding, no finding
above 1% severity, object unlocatable). Hitting one is a **successful outcome**, reported as
`status: negative` — a well-characterized "nothing further worth doing here" is a real result,
not a failure to try harder.
