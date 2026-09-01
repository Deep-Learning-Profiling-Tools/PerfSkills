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
    SKILL.md               collect-only, the first rung for a GPU workload

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
  schema/
    profile.schema.json         normalized profile — the cross-tool comparison contract
    benchmark.schema.json       timing campaign with correctness + significance
    rewrite-report.schema.json  a child's deliverable, including the trust gates
  scripts/
    bench.py               repeated timing, interleaved A/B, bootstrap CI, oracle check
    validate.py            schema validation (uses jsonschema if present, else built-in subset)
    tokens.py              skill-definition + end-to-end token cost, per agent
runs/                      one directory per investigation, id = YYYYMMDD-HHMMSS-<slug>
```

A run directory:

```
runs/<run-id>/
  brief.md                 the dispatch brief from the root
  preflight.json           tool version, permissions, baseline variance, oracle check
  raw/                     untouched profiler output — never deleted
  profile.baseline.json    normalized, schema-valid
  variants/<name>.patch
  bench.*.json             one record per timing campaign
  profile.variant.json     the reprofile
  cost.json                what the investigation consumed (tokens, per agent)
  report.json / report.md  the deliverable
```

## How routing works

The root filters children by **preconditions** (can this machine even run it?), then by
**insight** (can this granularity answer the question?), then sorts by **cost**
(overhead × runtime + rebuild + setup). It starts coarse and escalates only when the current
rung cannot answer — a narrowed scope from a cheap tool is what makes an expensive tool
affordable.

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
reports back. Invoke a child directly (`pw-perf`) when you already know which tool you want.

The shared scripts stand alone too:

```bash
python3 shared/scripts/bench.py --label variant-tiling --reps 7 \
    --baseline-command "./app in.deck" --oracle "python3 check.py out.dat ref.dat" \
    --out runs/<run-id>/bench.tiling.json -- ./app.tiled in.deck

python3 shared/scripts/validate.py shared/schema/profile.schema.json \
    runs/<run-id>/profile.baseline.json
```
