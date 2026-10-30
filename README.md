# ProfileWeaver

An agent skill tree for HPC performance work. A root skill turns a human question
("why is this slow?") into a measurement request, routes it to the cheapest tool expert that
can answer it, and adjudicates what comes back. Each child skill owns exactly one profiler and
runs the same closed loop:

```
profile → normalize → optimize → benchmark → reprofile → verify → report
```

The point is not to run profilers. It is to return **rewrites you can trust**: a change with a
measured, significant speedup, a passing correctness oracle, and a reprofile proving the
speedup came from where the diagnosis said it would.

ProfileWeaver separates the evidence plane from the agent plane. Raw traces, SQLite databases,
CSV files, full normalized profiles, and benchmark samples stay on disk. A deterministic
harness reduces them to `agent-brief.txt`: decisive measurements, exact source ranges, measured
counter and timeline deltas, and trust requirements. The machine sidecar, expanded diagnostic view, raw
evidence, and unchanged facts stay out of context. The agent, not ProfileWeaver, chooses and
combines source candidates.

Start a configured workload with one command:

```bash
python3 shared/scripts/experiment.py start --workload <directory> --iterations 10 --repairs 7
```

The workload directory may contain a `profileweaver.json` when it needs custom build arguments,
inputs, metrics, or correctness checks. If it is absent, `start` derives a conservative contract
from standard build files and the built executable; no benchmark-specific setup file is required.
If a standard `run` target exists, the inferred contract uses it; otherwise it runs the discovered
executable without inventing arguments. Use an explicit contract only when the workload requires
non-default invocation arguments or a semantic oracle.
The controller creates all run plans, probes tools once, builds and checks the baseline, profiles
it, selects the hotspot, and returns only a compact profile brief and bounded source slices.
During optimization, agents read those emitted files and edit candidate source only; framework
Python and run JSON are an internal API.

For code review, follow the start path in this order:

1. `shared/scripts/experiment.py` — public command parser and lifecycle dispatcher.
2. `shared/scripts/experiment_start.py` — workload-to-run startup orchestration.
3. `shared/scripts/start_manifest.py` — explicit manifest loading or conservative inference.
4. `shared/scripts/start_contract.py` — generated preflight and experiment plans.
5. `shared/scripts/experiment_state.py` — cached baseline initialization and persistent state.
6. `shared/scripts/experiment_bootstrap.py` — preflight, build, correctness, and baseline profile.

Long-running workloads may set `sampling.mode` to `winner-confirmation` and
`profiling.cadence` to `every-candidate` in their manifest. Each screening iteration then performs
one paired baseline/candidate execution plus an NSYS timeline delta and marks its result
exploratory; candidate NCU replay is deferred. After the iteration budget, only the best measured
candidate receives the configured multi-pair confirmation and full winner profiling. The standard
five-pair screening and per-candidate evidence path remains the default for other workloads.

## Works in both Claude Code and Codex

Both agents use the same skill format — a directory holding `SKILL.md` with `name` and
`description` frontmatter, plus optional `scripts/`, `references/`, `assets/`. Only the
discovery path differs, so the tree is exposed at both, from one set of files:

| | Claude Code | Codex |
|---|---|---|
| repo-local skills | `.claude/skills/` | `.agents/skills/` |
| repo instructions | `CLAUDE.md` | `AGENTS.md` |

`.agents/skills` is a symlink to `.claude/skills`, and `CLAUDE.md` is a symlink to `AGENTS.md`
— one source of truth each, nothing to keep in sync. Edit `.claude/skills/` and `AGENTS.md`.

Skills are written to the common denominator: plain markdown, paths relative to the repo root,
scripts invoked as `python3 <path>`, and no agent-specific variables like `${CLAUDE_SKILL_DIR}`.

## Layout

```
.claude/skills/
  profile-weaver/          root dispatcher — framing, routing, adjudication
    SKILL.md
    references/
      routing.md           bottleneck taxonomy, cost model, escalation ladder
      registry.md          installed children + their cost/insight/preconditions  ← hand-edited
  pw-perf/                 child: Linux perf (worked example)
    SKILL.md
    references/tool-notes.md
  pw-gvprof/               child: GVProf GPU redundancy — PARTIAL, wraps external/
    SKILL.md
    references/tool-notes.md
  pw-gpa/                  child: GPA/HPCToolkit CUDA PGO — wraps external/
    SKILL.md
  pw-drgpum/               child: DrGPUM GPU *memory* — wraps external/; optimizes peak
    SKILL.md               bytes, not time
  pw-hpctoolkit/           child: stock HPCToolkit GPU profiling — wraps external/;
    SKILL.md               collect-only calling-context and source attribution
  pw-nsight-systems/       child: NVIDIA Nsight Systems host/device timeline — live capture;
    SKILL.md               GPU gaps, transfers, launch overhead, sync, and overlap
    references/
      tool-notes.md        trace providers, metric mapping, interval math, and pitfalls
  pw-nsight-compute/       child: bounded NVIDIA Nsight Compute full-set replay;
    SKILL.md               registers, occupancy, warps/stalls, cache/traffic, pipes, rules
.agents/skills  ─────────> symlink to .claude/skills, so Codex sees the same tree
AGENTS.md                  repo instructions for Codex; CLAUDE.md is a symlink to it
templates/child-skill/     copy this to build a new child
external/                  vendored upstream code a child wraps — never edited casually
  gvprof-agent-skill/      separate git repo (Honrrii/gvprof-agent-skill)
  gpa-agent-skill/         the GPA PGO loop: nine steps, a KEPT/REVERTED ledger, and a
                           cross-benchmark prior. Needs $PW_GPA_ROOT (a built GPA install)
  drgpum-agent-skill/      the DrGPUM memory pipeline (4 skills) + its `agent/` package,
                           pruned to the core skill; see its PROVENANCE.md. Needs a DrGPUM
                           install to capture profiles
  hpctoolkit-agent-skill/  the HPCToolkit collect workflow + a matmul example; no upstream
                           git, so ProfileWeaver tracks it. See its PROVENANCE.md
shared/
  config/registry.json          machine-readable routing registry
  schema/
    profile.schema.json         normalized profile — the cross-tool comparison contract
    benchmark.schema.json       timing campaign with correctness + significance
    rewrite-report.schema.json  a child's deliverable, including the trust gates
    agent-view.schema.json      expanded diagnostic view for named drill-downs
    agent-brief.schema.json     decision-only machine sidecar
    experiment-plan.schema.json inherited run plus routed-child iteration-profile contract
    campaign-plan.schema.json   internal workload-agnostic command/metric/oracle campaign
    campaign-result.schema.json complete evidence-plane campaign result
    preflight-plan.schema.json  cached compiler/device/profiler/build/launch contract
    preflight-result.schema.json compact capability result and failure classification
    batch-plan.schema.json      quiet multi-candidate execution batch
    profile-diff.schema.json    impact-ranked operation and source-line deltas
    ncu-diagnostics.schema.json bounded kernel-internal diagnostic plus drill-down manifest
    source-targets.schema.json  bounded profiler-selected code ranges
    harness-plan.schema.json    resumable execution plan
  scripts/
    experiment.py          single public optimization CLI
    experiment_*.py        routing, state, campaign, inspection, and reporting backend
    kernel_*.py            filtered NCU capture, evidence reduction, and advice hierarchy
    profile_*.py           profiler cadence and compact before/after deltas
    source_*.py            bounded hotspot, callee, data, and sibling-variant context
    campaign_*.py          paired sampling, gates, and winner selection backend
    brief_*.py             token-bounded decision capsule backend
    bench.py               correctness-aware timing CLI used by the controller
    preflight.py           cached compiler/device/profiler/build/launch checks
    validate.py            schema validation
    tokens.py              measured skill/session cost
runs/                      one directory per investigation, id = YYYYMMDD-HHMMSS-<slug>
```

A run directory:

```
runs/<run-id>/
  brief.md                 the dispatch brief from the root
  plan.json                resumable execution plan
  harness-state.json       input/output hashes and stage status
  preflight.json           tool version, permissions, baseline variance, oracle check
  raw/                     untouched profiler output — never deleted
  profile.baseline.json    normalized, schema-valid
  source-targets.json      measured hotspots mapped to bounded source ranges
  source-slices/*.txt      only code context loaded by the optimizer
  variants/<name>.patch
  bench.*.json             one record per timing campaign
  candidates/<id>/profile/profile.json
                           normalized profile from the routed child for this correct candidate
  candidates/<id>/profile/profile-diff.json
                           impact-ranked iteration delta and remaining leverage
  candidates/<id>/profile/source-targets.json
                           next hotspot mapped to a bounded candidate source slice
  experiment-plan.json     one inherited contract for every candidate
  experiment-state.json    compact candidate, batch, and winner ledger
  campaign-plan.json       controller-generated final campaign contract
  campaign-result.json     complete candidate and finalist samples and decisions
  agent-brief.txt          normal agent input (default ≤320 exact Codex tokens)
  agent-brief.json         evidence-plane measurements, frontier, and provenance
  agent-view.json          expanded diagnostic view for named drill-downs
  cost.json                what the investigation consumed (tokens, per agent)
  report.json / report.md  the deliverable
```

## How routing works

The root filters children by **preconditions** (can this machine even run it?), then by
**insight** (can this granularity answer the question?), then sorts by **cost**
(overhead × runtime + rebuild + setup). It starts coarse and escalates only when the current
rung cannot answer — a narrowed scope from a cheap tool is what makes an expensive tool
affordable.

The experiment plan declares discovery and diagnostic capability needs. `experiment.py init`
uses `shared/config/registry.json` internally, records separate phase choices in
`profiler-routing.json`, and selects by coverage, availability, then measured/configured cost.
`route.py` remains a maintainer diagnostic; routine agents do not invoke it.

See [routing.md](.claude/skills/profile-weaver/references/routing.md) for the taxonomy, cost
bands, and the five-rung escalation ladder.

## Trust gates

A child's report is only `verified` when all six pass:

| Gate | Passes when |
|---|---|
| Correctness | the oracle passed on the variant, tolerance stated |
| Significance | speedup CI excludes 1.0, reps ≥ 5, conditions matched |
| Mechanism | the reprofile shows the targeted hotspot shrinking |
| No collateral | nothing else regressed unexplained |
| Attribution | every causal claim cites a metric the tool actually measures |
| Reproducible | command, build, host, tool version, run id recorded |

Anything less is reported as `proposed`, never as a speedup. A well-characterized negative
result is a real deliverable.

## Adding a child

1. `cp -r templates/child-skill .claude/skills/pw-<tool>`
2. Fill every TODO in `SKILL.md` and `references/tool-notes.md`. Keep the seven stages and the
   gate table — children differ in tool mechanics, not in method.
3. Add a row to [registry.md](.claude/skills/profile-weaver/references/registry.md). **A child
   not in the registry will not be routed to.**
4. Update its declared cost with the observed overhead after the first real run.

The registry's "candidate children" table lists the suggested build order, each entry covering
a column of the taxonomy the installed set cannot currently reach.

### Wrapping an existing tool repo

When a child is backed by an upstream project, vendor it under `external/` and keep the child
skill a thin wrapper in `.claude/skills/`. Do not drop the upstream repo into
`.claude/skills/` — that directory is scanned for `SKILL.md` at each child's root, so a repo
without one registers as nothing at all, and skills it ships one level deeper (in its own
`.claude/skills/` or `.agents/skills/`) are not reached from this repo's root either.

The wrapper's job is translation: delegate mechanics to upstream's CLI, normalize its output to
`profile.schema.json`, and report against the gates. Leave upstream files untouched so a later
pull does not silently revert local edits. `pw-gvprof` is the worked example.

### Partial children

Not every tool can run all seven stages. `pw-gvprof` reads pre-captured artifacts (no stage 2)
and reports a *proxy* metric with no time dimension. That is fine — but it must be declared in
the registry, because it changes which gates can pass. A child that cannot reprofile can never
pass the `mechanism` gate on its own, so its honest ceiling is `status: proposed`. Declare the
limit; do not let the loop imply a completeness the tool does not have.

## Using it

Ask the root skill a performance question. The agent starts the workload, then interacts with one
entry point for the entire journey. It passes the returned controller path without reading it:

```bash
python3 shared/scripts/experiment.py start --workload workloads/example --iterations 10 --repairs 7
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json prepare-batch \
    --candidate candidate-a:baseline:search-structure
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json evaluate candidate-a
# after the verdict, use --candidate candidate-b:current:<next-mechanism>
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json confirm candidate-a
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json status
```

`start` creates the internal plans and performs cached phase routing and baseline setup. A typical NVIDIA kernel journey routes
baseline discovery to NSYS and unresolved kernel internals to NCU; CPU, calling-context,
memory-capacity, and artifact-based questions select other children. NCU is never selected merely
because an executable uses CUDA.

For FP32/FP64 kernels with the required NCU counters, the compact diagnostic follows NVIDIA's
SpeedOfLight Roofline equations, derives a kernel opportunity ceiling, and passes that ceiling
through the timing profiler's hotspot fraction with Amdahl's law. It reports the hierarchy as a
bound with its inputs and missing fields; benchmark and reprofile results remain the outcome.

NSYS or another timing child discovers the baseline hotspot once. Unprofiled CUDA-event or
application ROI timing ranks candidates, while whole-process time checks collateral cost. Every
correct changed kernel receives a bounded, filtered NCU capture and parent/baseline delta in the
standard mode; an unchanged fingerprint explicitly reuses evidence. Verified NCU section names
drive a diagnosis-specific metric tier. Default confirmation reuses the paired application
samples and candidate NCU delta already collected during evaluation, so it launches no new work.
Use `confirm --fresh` only when an unresolved attribution or collateral-cost gate requires a new
timeline. Winner-confirmation workloads with `every-candidate` cadence retain NSYS deltas during
screening but defer candidate NCU replay until the winner is known.

The compact result includes profiling time, duration, registers, occupancy/resource limits,
scheduler eligibility and issue activity, leading stalls, memory transaction efficiency, DRAM
bytes, cache and throughput evidence, instruction/control-flow signals, analytical bounds, and
ranked structural mechanisms. Full captures stay on disk. Request one bounded diagnostic only
when necessary:

```bash
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json inspect \
    --section ncu --candidate candidate-a --group memory
```

The source locator emits the measured hotspot plus a bounded chain of callees, accessed-object
declarations, builders/writers, and transfers. A candidate may make several linked edits across
those slices when they test one coherent mechanism. This supports structural changes such as
search/index replacement or data-layout transformation without broad codebase exploration.
When the declared source tree already contains named sibling implementations of the measured
function or its driver, the locator may also emit a few `existing-variant` slices. These may come
from the measured file or a sibling source file. They are bounded source references for discovering
a structural alternative; they are not profiler evidence and never replace correctness or paired
timing gates. The text brief also carries a compact dependency frontier so a multi-file
producer/transfer/consumer path remains addressable without dumping source or raw traces.

The mechanism catalog is benchmark-agnostic. Counter and syntax facts are reduced to portable
classes such as transaction waste, dependent lookup, data layout, residency, and work reuse.
Workload-specific names may appear only where they are required to identify an emitted source
slice or a measured kernel. Candidate descriptions supplied on the command line are agent input,
not ProfileWeaver hypotheses; the controller preserves them for audit but does not use their
domain vocabulary to invent new mechanisms.
