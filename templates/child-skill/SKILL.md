---
name: pw-TOOL
description: TODO one paragraph — the tool this child owns, the bottleneck classes it resolves, the granularity of its attribution, and its cost. Written so the root dispatcher can route on it. Name the tool and its common aliases explicitly so direct user requests ("profile this with TOOL") also land here.
---

# pw-TOOL — TODO tool name

Expert for **TODO tool**. You own one tool and one loop. You are normally invoked by
`profile-weaver` with a brief, but you must work from a bare request too — in that case build
the brief yourself before doing anything (question, workload, oracle, budget).

<!--
FILLING THIS IN
Replace every TODO. Delete this comment. Keep the seven stages and the two gate sections
verbatim in spirit — they are the contract the root dispatcher adjudicates against. What
changes between children is *tool mechanics*, not method.

Copy this directory to .claude/skills/pw-<tool>/ and add a registry row. That one location
serves both agents: Claude Code reads .claude/skills/, and Codex reads .agents/skills/, which
is a symlink to it.

Stay agent-neutral so this works in both:
  - `name` and `description` are the only frontmatter both agents require. `allowed-tools:` is
    honored by Claude Code and ignored by Codex - fine to use, never rely on it.
  - Write paths relative to the repo root and say commands are run from there.
  - No agent-specific variables (${CLAUDE_SKILL_DIR} and the like). Spell the path out.
  - Invoke helpers explicitly: `python3 .claude/skills/pw-<tool>/scripts/x.py`.
-->

## Declared capability

Keep this in sync with the row in `profile-weaver/references/registry.md`.

- **Answers**: TODO bottleneck classes from `routing.md`
- **Cannot answer**: TODO classes to hand back to the router rather than guess at
- **Granularity**: TODO function / line / instruction / kernel / rank / file
- **Overhead**: TODO measured factor, and what drives it
- **Requires**: TODO permissions, build flags, hardware, modules, license
- **Escalates to**: TODO which child takes over when this one bottoms out

## Stage 1 — Preflight

Do not profile until all of these hold. Each failure has a fix or a hand-back; never proceed
on a broken premise and annotate it later.

1. **Tool present and version pinned.** `TODO --version`. Record it — the normalized output
   carries it, because tool versions change metric definitions.
2. **Permissions.** TODO the specific check (e.g. `cat /proc/sys/kernel/perf_event_paranoid`,
   GPU counter access flag, container capability). If unavailable and unfixable, hand back to
   the router with the reason; do not silently downgrade to a weaker measurement.
3. **Build is profilable.** TODO required flags (`-g`, `-fno-omit-frame-pointer`, `-lineinfo`).
   If a rebuild is needed, do it and confirm it did not change the workload semantics.
4. **Baseline with variance.** Time the unmodified workload ≥5 reps:
   `python3 shared/scripts/bench.py --label baseline --reps 5 -- <run cmd>`
   Record median and relative MAD. **If the noise floor exceeds the effect you are chasing,
   stop.** Report the noise problem — a rewrite cannot be validated on this machine until it
   is fixed (pinning, exclusive allocation, fixed clocks, more reps).
5. **Oracle is executable.** Run the correctness check against the baseline output and confirm
   it passes on an unmodified build. An oracle you never saw pass proves nothing later.

Write all of this to `runs/<run-id>/preflight.json`.

## Stage 2 — Profile

Run the tool with the smallest scope that answers the question.

```
TODO exact command, with the flags that matter and why each is there
```

Rules:

- **Scope down first.** If the brief narrowed to a region/kernel/rank, restrict the tool to it.
  Whole-program runs of an expensive tool are a routing failure.
- **Don't tune the sampling rate for prettiness.** State it, and make sure the hot region got
  enough samples that its share is statistically meaningful (TODO tool-specific rule of thumb).
- **Profile the same build you benchmarked.** If profiling required different flags, note it —
  the profile describes that build, not the production one.
- Keep raw output in `runs/<run-id>/raw/`. Never delete it; the normalization is an
  interpretation and someone may need to re-derive it.

## Stage 3 — Normalize

Convert raw output to `shared/schema/profile.schema.json` at
`runs/<run-id>/profile.baseline.json`, then validate:

```
python3 shared/scripts/validate.py shared/schema/profile.schema.json runs/<run-id>/profile.baseline.json
```

Normalization is where most dishonesty enters. Guard it:

- **Self vs. total time**: map the tool's terms to the schema's explicitly. Never sum self and
  total into one column.
- **Units**: schema is seconds, bytes, and counts. Convert; do not pass through the tool's
  native units.
- **Percentages** are of the schema's stated denominator (`totals.wall_time_s`), not of
  whatever subset the tool displayed.
- **Unattributed time** goes in `warnings` with its magnitude. Broken unwinds, missing symbols,
  and `[unknown]` frames are findings, not rounding errors.
- **Do not invent fields.** If the tool cannot measure something the schema allows, leave it
  null. A null is information; a plausible guess is contamination.

Set `bottleneck.class`, `bottleneck.evidence[]` (each entry citing a metric this tool actually
measured), and `bottleneck.confidence`. If the evidence does not distinguish between two
classes, say so and hand back for triangulation rather than picking the likelier one.

## Stage 4 — Attach optimization

For the top hotspot(s), read the actual source and propose a change whose mechanism matches the
diagnosed class. The link must be explicit: *this metric* → *this cause* → *this change* →
*this predicted effect*.

- **Predict before measuring.** Write the expected speedup and its reasoning into the report
  first. A prediction that misses badly means the diagnosis was wrong even if the code got
  faster — that is the single most valuable signal in this loop, so do not quietly drop it.
- **One variable per variant.** Separate algorithmic changes from flag changes from layout
  changes. Bundled changes measure nothing.
- **Bound the blast radius.** Prefer local, reviewable changes. Note any change to numerics,
  API, memory footprint, or portability.
- **Respect the ceiling.** Compute the Amdahl bound from the profile: a region at 12% of wall
  time cannot yield 2× overall. State the ceiling next to the prediction; if the ceiling is
  uninteresting, say so and hand back rather than optimize it anyway.

Keep each variant as a patch in `runs/<run-id>/variants/<name>.patch`.

## Stage 5 — Benchmark

For every variant, in this order:

1. **Correctness first.** Run the oracle. A failing variant is dead — record it and move on;
   do not report its timing.
2. **Same conditions as baseline**: same node, same launch shape, same input, same build flags
   except the change under test. Interleave variant and baseline runs (A/B/A/B) so drift shows
   up as noise rather than as a result.
3. **Reps**: ≥5, more if variance is high. Report median, MAD, and a bootstrap CI on the
   speedup — never a single run, never the minimum.

```
python3 shared/scripts/bench.py --label variant-NAME --reps 7 --baseline runs/<run-id>/bench.baseline.json -- <run cmd>
```

Output conforms to `shared/schema/benchmark.schema.json`.

**If the speedup CI includes 1.0, the variant did nothing measurable.** Say that. Do not
promote it on the strength of the point estimate.

## Stage 6 — Reprofile

Re-run stage 2 on the winning variant, normalize to `profile.variant.json`, and diff:

- Did the **targeted** hotspot's share actually fall? If wall time improved but the target did
  not shrink, you do not understand the change — investigate before reporting.
- Did the bottleneck **class shift** (e.g. memory-bound → latency-bound)? That is the next
  lever; record it.
- Did anything **else regress**? A win funded by a hidden loss elsewhere is not a win.
- Did new time appear as `[unknown]`/unattributed? Then attribution broke; fix it before
  claiming a mechanism.

## Stage 7 — Report

Emit `runs/<run-id>/report.json` per `shared/schema/rewrite-report.schema.json`, plus a
human-readable `report.md`. The root dispatcher adjudicates against the gates below, so state
each one's result explicitly rather than leaving it to be inferred.

### Trust gates — all must pass to claim a verified rewrite

| Gate | Passes when |
|---|---|
| Correctness | Oracle passed on the variant, with tolerance stated |
| Significance | Speedup CI excludes 1.0, reps ≥5, conditions matched |
| Mechanism | Reprofile shows the targeted hotspot shrinking |
| No collateral | No unexplained regression elsewhere |
| Attribution | Every causal claim cites a metric this tool measures |
| Reproducible | Run command, build, node, tool version, run id all recorded |

Any gate failing downgrades the result to `proposed` — still worth reporting, but never as a
verified speedup. Report honestly which gate failed; a well-characterized negative result is a
real deliverable and prevents the next agent from repeating the work.

### Say what you cannot see

Close every report with the limits of *this tool*: what it samples, what it cannot observe,
where its attribution is approximate, and which child skill would answer the leftover question.

## Tool notes

Deeper mechanics — flag reference, output format quirks, metric definitions, known pitfalls —
live in `references/tool-notes.md`. Read it before stage 2.
