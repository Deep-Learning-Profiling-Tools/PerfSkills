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
harness reduces them to `agent-brief.txt`: only the next action, decisive measurements, exact
source ranges, search frontier, and trust requirement. The machine sidecar, expanded diagnostic
view, raw evidence, and unchanged facts stay out of context.

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
    harness.py             stage cache, complete logs, brief plus diagnostic view
    batch_harness.py       several independent plans, one agent exchange
    brief.py               token-bounded, workload-agnostic brief and opportunity frontier
    experiment.py          workspaces, batches, per-candidate profile deltas and confirmation
    campaign.py            internal builds, oracle checks, paired trials and finalists
    finalize.py            profile delta, trust-gate report, validation, final brief
    preflight.py           cached compiler/device/profiler/build/launch checks
    agent_view.py          expanded diagnostic view plus named drill-downs
    session_audit.py       rollout token/tool-output reduction without transcript loading
    route.py               availability/capability/cost routing without loading prose
    locate_source.py       profiler-selected bounded source slices
    diff_profiles.py       impact-ranked before/after and honest line deltas
    learn.py               guarded cost/outcome priors after three runs
    bench.py               paired adaptive timing, bootstrap CI, oracle check
    validate.py            schema validation (uses jsonschema if present, else built-in subset)
    tokens.py              skill-definition + end-to-end token cost, per agent
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

Normal routing uses `shared/config/registry.json` through `route.py`; the markdown registry is
the detailed human reference. `route.py --check-registry` verifies both registries and installed
skill directories agree.

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

Ask the root skill a performance question — `profile-weaver` frames it, picks a child, and
reports back. Invoke a child directly (`pw-perf` or `pw-nsight-systems`) when you already know
which tool you want.

The shared scripts stand alone too:

```bash
python3 shared/scripts/bench.py --label variant-tiling --reps 7 \
    --baseline-command "./app in.deck" --oracle "python3 check.py out.dat ref.dat" \
    --out runs/<run-id>/bench.tiling.json -- ./app.tiled in.deck

python3 shared/scripts/validate.py shared/schema/profile.schema.json \
    runs/<run-id>/profile.baseline.json

python3 shared/scripts/harness.py runs/<run-id>/plan.json --learn

python3 shared/scripts/preflight.py runs/<run-id>/preflight-plan.json

python3 shared/scripts/brief.py runs/<run-id> --print-brief

python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json init

python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json prepare-batch \
    --candidate candidate-a:baseline:loop-fusion \
    --candidate candidate-b:baseline:invariant-precompute

python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json evaluate candidate-a candidate-b

python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json confirm candidate-a
```

`evaluate` benchmarks each candidate, profiles every correct candidate once with the child chosen
by the router, and returns a bounded delta capsule. When NCU is enabled, it automatically captures
changed kernel evidence or explicitly reuses unchanged parent evidence, returning parent- and
baseline-relative inefficiency deltas. Use `--ncu ID` only to force a replay. Full captures and
command streams remain under `raw/iteration/<candidate>/`; normalized evidence and named source slices remain under the
candidate profile directory. `confirm` reuses that cached delta. A timing-confirmed winner receives
a fresh final reprofile before the final verdict.
