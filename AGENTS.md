# ProfileWeaver

An agent skill tree for HPC performance work. A root skill (`profile-weaver`) turns a human
question into a measurement request, routes it to the cheapest tool expert that can answer it,
and adjudicates what comes back. Each child skill (`pw-*`) owns exactly one profiler.

The deliverable is never "it got faster." It is a **rewrite you can trust**: a measured,
significant improvement, a passing correctness oracle, and a reprofile proving the gain came
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

1. Record the exact build, command, input, environment, oracle, metric, and baseline fingerprint.
2. Route to the cheapest capable child listed in
   `.claude/skills/profile-weaver/references/registry.md`. Never invent measurements or attribute
   a mechanism the selected profiler cannot observe; unknown values remain `null`. Invoke
   `shared/scripts/route.py`; do not read the registry, automation reference, or schemas during a
   routine run.
3. Consume only compact briefs, per-iteration delta capsules, and their named source slices.
   Resolve one named dependency at a time; never browse the application, raw traces, full
   profiles, logs, or large JSON broadly.
4. Profile every correct candidate once with the routed timing child. Let the controller cache
   the capture, normalize and diff it, and advance the measured hotspot frontier; confirmation
   reuses that evidence and the accepted winner receives a fresh final reprofile.
5. For NVIDIA kernel-internal questions, use `pw-nsight-systems` to name and time one hotspot,
   then `pw-nsight-compute --section all` for that bounded kernel. Every correct candidate must
   return an NCU disposition: capture when its kernel-evidence fingerprint changes, otherwise
   explicit reuse. Consume parent- and baseline-relative deltas from the ≤480-token brief or one
   named metric group, never the full counter table. Freshly replay the accepted winner.
6. Work on the largest measured hotspot. Screen up to three orthogonal structural hypotheses per
   batch and change one variable per candidate. Preserve numerical and communication semantics.
7. Use `shared/scripts/experiment.py` for candidate preparation, evaluation, confirmation, and
   finalization. Edit candidate source only. Do not write inline or run-local orchestration,
   polling, inspection, summary, validation, or reporting programs. Declare `kernel_profile`
   once; let `init` collect baseline NCU and `evaluate` automatically capture or reuse evidence
   for every candidate. Use `--ncu ID` only to force a replay. Let confirmation freshly profile
   the winner. Never create or patch a harness plan for NCU.
8. For long commands, wait on the existing session at the longest practical interval. Do not
   combine waits with `tail`, `ps`, JSON readers, or raw-log reads. Prefer non-login `/bin/sh`;
   establish any required module environment once in cached preflight.
9. Accept only a correct, significant absolute improvement under the same fingerprint whose
   reprofile shows the targeted cost fell and collateral cost did not erase the gain. An
   unresolved gate is `inconclusive`, never a speedup.
10. Keep detailed application/profiler output under `raw/`. Return one compact verdict and artifact
   paths. Record cost with `shared/scripts/tokens.py`.
