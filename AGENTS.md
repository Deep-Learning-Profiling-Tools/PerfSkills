# ProfileWeaver

An agent skill tree for HPC performance work. A root skill (`profile-weaver`) turns a human
question into a measurement request, routes it to the cheapest tool expert that can answer it,
and adjudicates what comes back. Each child skill (`pw-*`) owns exactly one profiler.

The deliverable is never "it got faster." It is a **rewrite you can trust**: a measured,
significant improvement, a passing correctness oracle, and profiler deltas proving the gain came
from where the diagnosis said it would.

## Works in both Claude Code and Codex

The two agents use the same skill format — a directory holding `SKILL.md` with `name` and
`description` frontmatter, plus optional `scripts/`, `references/`, `assets/`. Only the
discovery path differs, so this repo exposes one set of skills at both:

```
.claude/skills/          real directory — the source of truth (Claude Code)
.agents/skills/  ─────>  symlink to ../.claude/skills   (Codex)
```

Edit skills in `.claude/skills/`. The symlink means Codex sees the same files with no copying
and nothing to keep in sync. Codex scans `.agents/skills` from the working directory up to the
repo root, so this works from anywhere in the tree.

If a skill ever fails to load under Codex, check the symlink survived (`ls -l .agents/skills`);
replacing it with `cp -rL .claude/skills .agents/skills` is the fallback, at the cost of having
two copies to maintain.

Write skills to the common denominator: plain markdown, relative paths, and scripts invoked as
`python3 <path>`. Do not depend on agent-specific variables such as `${CLAUDE_SKILL_DIR}` —
paths in this repo are written relative to the repo root, and commands are meant to be run from
there. `allowed-tools:` in a few children is honored by Claude Code and ignored by Codex, which
is harmless.

## Rules that hold regardless of agent

1. **A child not listed in `.claude/skills/profile-weaver/references/registry.md` will not be
   routed to.** Adding a skill directory is half the job; registering it is the other half.
2. **Never report a performance number you did not measure**, or that a child did not measure
   and record under a run id. No estimated speedups.
3. **Never let a rewrite ship without the correctness oracle passing.** A faster wrong answer
   is not an optimization.
4. **Never attribute a cause to a mechanism the tool cannot observe.** A sampler does not see
   cache misses; a memory profiler does not see time.
5. **One variable per benchmark.** A change that touches the algorithm and the flags at once
   measured nothing.
6. **A null is information; a plausible guess is contamination.** Leave unmeasured fields null
   in normalized output.
7. **Report what the investigation cost.** Close each run with
   `python3 shared/scripts/tokens.py run <run-id> --loaded <skill>:<n>`. Skill-definition
   tokens are measurable from the repo; end-to-end session tokens are not introspectable and
   must come from `/cost` — if unsupplied they stay null. Counting is per-agent (Anthropic
   `count_tokens` under Claude, `tiktoken o200k_base` under Codex) and the two are not
   comparable.
8. **Do not edit `external/` casually.** Those are vendored upstream trees; some carry their
   own git, some are the only copy in existence. See each one's `PROVENANCE.md`.

## Layout

| Path | What |
|---|---|
| `.claude/skills/profile-weaver/` | root dispatcher: framing, routing, adjudication |
| `.claude/skills/pw-*/` | one child per profiler, mostly thin wrappers over `external/` |
| `templates/child-skill/` | copy this to add a child |
| `external/` | vendored upstream skills and packages the children wrap |
| `shared/schema/` | the cross-tool contracts: profile, benchmark, rewrite-report |
| `shared/scripts/` | `bench.py` (timing + stats), `validate.py` (schema check), `tokens.py` (skill + session token cost) |
| `runs/` | one directory per investigation, `YYYYMMDD-HHMMSS-<slug>` |

Start at `.claude/skills/profile-weaver/SKILL.md`. `README.md` has the full picture.

## Efficient optimization workflow

Optimize from measured evidence with minimal agent context.

1. Invoke `python3 shared/scripts/experiment.py start --workload <directory>`; an optional
   `profileweaver.json` supplies non-default build, command, oracle, metric, or profiling facts.
   When it is absent, the backend derives a conservative contract from standard build files and
   the executable. Never hand-author or edit run JSON.
2. Invoke only `shared/scripts/experiment.py`; `start` routes profilers, runs cached preflight,
   builds and validates the baseline, captures NSYS and NCU, and records the decision. Never infer
   a mechanism a selected profiler cannot observe; unknown values remain `null`.
3. Read only compact profile briefs, per-iteration delta capsules, and their named source slices.
   Edit only returned candidate source workspaces. Never read framework Python, schemas, plans,
   state, README files, child `pw-*` skill files, raw traces, full logs, or unrelated benchmark
   source.
   If the brief names `existing-variant` source slices, read those bounded sibling implementations
   (which may be in another declared source file) as structural references; they are not measurements
   and do not justify skipping correctness or paired timing gates. Use the compact dependency frontier
   to follow producer/transfer/consumer edges without browsing unrelated source.
4. Use a timing profiler once to discover the baseline hotspot. Rank candidates with the declared
   unprofiled metric, preferably CUDA-event or application ROI time for a kernel objective, and
   retain whole-process time as a collateral check. Re-run the full timing profile only when an
   attribution or collateral-cost gate remains unresolved.
5. For NVIDIA kernel-internal questions, use baseline NSYS to name and time one hotspot, then
   baseline NCU full evidence. Every correct candidate in standard mode returns an NCU capture/reuse
   disposition and parent- plus baseline-relative deltas. A workload using
   `sampling.mode=winner-confirmation` returns timing deltas during screening and defers candidate
   replay until the best candidate is selected; its final winner campaign restores the full NCU
   evidence and multi-run gates. Candidate replay uses only diagnosis-relevant NCU
   sections verified by preflight; fall back to full evidence when a safe tier is unavailable.
   Reuse the winner's paired benchmark and candidate NCU evidence by default.
6. Use the largest measured remaining cost and the emitted source/dataflow evidence to design the
   candidate. Prepare and evaluate exactly one candidate per step to keep the returned evidence
   compact; this is not a restriction on the agent's search. A candidate may contain several
   linked edits across the bounded hotspot/callee/data dependency slices, including cross-file
   producer/consumer changes. After the compact verdict, use the accepted measured candidate as
   the next parent, or deliberately branch from any preserved parent after a regression. Sibling
   references and GPA classifications are descriptive evidence; never reject or defer an
   agent-proposed candidate because it repeats, departs from, or is absent from them. Preserve
   numerical and communication semantics. ProfileWeaver never authors or applies source edits:
   it emits measured deltas and bounded source evidence, while Codex supplies the candidate
   mechanism, parent, and edits.
7. Use `shared/scripts/experiment.py` for start, inspection, candidate preparation,
   evaluation, confirmation, and finalization. Edit candidate source only. Do not write inline or
   run-local orchestration, polling, inspection, summary, validation, or reporting programs.
   Do not repair framework code during a journey. Let the backend create contracts, collect,
   reduce, cache, diff, and finalize.
8. For long commands, wait on the existing session at the longest practical interval. Do not
   combine waits with `tail`, `ps`, JSON readers, or raw-log reads. Prefer non-login `/bin/sh`;
   establish any required module environment once in cached preflight.
9. Accept only a correct, significant absolute improvement under the same fingerprint whose
   candidate profile shows the targeted cost fell and collateral cost did not erase the gain. An
   unresolved gate is `inconclusive`, never a speedup.
10. Keep detailed application/profiler output under `raw/`. Return one compact verdict and artifact
   paths. Record cost with `shared/scripts/tokens.py`.
