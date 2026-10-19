---
name: pw-TOOL
description: TODO Name the one profiler, the bottleneck classes it observes, attribution granularity, and when to use it. Include common aliases so direct requests route here.
---

# pw-TOOL

Own **TODO tool** and its automated loop. Work from a ProfileWeaver request or create the same
question/workload/oracle/budget contract from a direct request.

## Capability

Keep this synchronized with `profile-weaver/references/registry.md` and
`shared/config/registry.json`.

- **Answers**: TODO taxonomy tags
- **Cannot answer**: TODO mechanisms that require another tool
- **Granularity**: TODO function/line/instruction/kernel/rank/file
- **Measured cost**: TODO overhead and dominant setup cost
- **Requires**: TODO tool, hardware, permissions, build flags, artifacts
- **Escalates to**: TODO child and exact evidence handed over

## Automated loop

Create `runs/<run-id>/plan.json` per `shared/schema/harness-plan.schema.json`, then execute:

```bash
python3 shared/scripts/harness.py runs/<run-id>/plan.json --learn
```

Use separate cacheable stages so parser changes do not repeat collection:

1. `preflight`: confirm version, permissions, workload, ROI/scope, oracle, and expected cost.
2. `baseline-benchmark`: at least five correct unprofiled samples.
3. `capture-baseline`: the smallest profiler scope that answers the question.
4. `normalize-baseline`: write and validate `profile.baseline.json`.
5. `locate-source`: emit bounded source ranges for measured hotspots.
6. `build-variant`: one source or configuration change.
7. `benchmark-variant`: paired adaptive A/B with the oracle.
8. `reprofile-winner`: same profiler settings and scope.
9. `report`: adjudicate every trust gate.

Full stdout/stderr and raw profiler data stay under the run directory. Stage stdout must be a
compact status object; detailed evidence belongs in declared output files.

## Preflight and capture

TODO exact `--version`, permission, and capability-discovery commands. Treat installed help as
authoritative. Inspect the existing build and launcher. Preserve input, ranks, threads,
affinity, device mapping, flags, libraries, warmups, and environment.

Run the baseline oracle before profiling. If permissions or required attribution are absent,
return a blocker or reroute; do not silently substitute weaker evidence.

TODO minimal capture command and why each non-default option is required. Scope to the prior
kernel/region/rank when present. Save immutable raw output under `raw/`. Measure actual overhead
on the workload rather than retaining a routing estimate.

## Normalize and locate

Write a deterministic reducer under this skill's `scripts/` when the tool output needs parsing.
It must emit `profile.baseline.json` and a compact status line. Convert units to seconds, bytes,
and counts; name denominators; distinguish self from inclusive cost; record unattributed data;
and leave unobserved fields null.

Validate:

```bash
python3 shared/scripts/validate.py shared/schema/profile.schema.json \
  runs/<run-id>/profile.baseline.json
```

Resolve only profiler-selected source:

```bash
python3 shared/scripts/locate_source.py \
  --profile runs/<run-id>/profile.baseline.json --run-dir runs/<run-id> \
  --source-root <application-source-root>
```

Read only the emitted slices. Resolve one named dependency with `--symbol NAME --append` if the
initial range cannot establish correctness or explain a proposed change. Low-confidence or
unresolved mapping is a blocker or an escalation signal.

## Change, benchmark, reprofile

Connect a measured metric to one predicted change. Respect its Amdahl ceiling. Store one change
per patch and preserve the baseline.

Define one `experiment-plan.json` and use `shared/scripts/experiment.py` for candidate
preparation, batched screening, confirmation, and finalization. Edit candidate source only. Do
not generate inline or run-local preparation, plan, polling, summary, report, or validation
programs. Application output belongs under `raw/`.

```bash
python3 shared/scripts/bench.py --label variant-NAME --adaptive \
  --min-reps 5 --max-reps 15 --min-speedup 1.02 \
  --baseline-command '<baseline command>' --oracle '<oracle>' \
  --compact-stdout --out runs/<run-id>/bench.variant-NAME.json -- <variant command>
```

Reject a failed oracle. Reprofile only a statistically meaningful winner using identical tool
settings. Verify the targeted measured cost moved as predicted and inspect bounded profile
diffs for collateral regression.

## Report contract

Write `report.json` per `shared/schema/rewrite-report.schema.json`. A rewrite is `verified`
only when correctness, significance, mechanism, no-collateral, attribution, and reproducibility
all pass. Otherwise use `proposed`, `negative`, or `blocked` and state the failed gate.

The agent consumes the token-bounded `agent-brief.txt`, then only decision-changing named
drill-downs. Close with tool limitations and the exact child that can measure the remaining
question.

## Tool notes

Put version-sensitive flags, metric definitions, output quirks, and failure recovery in
`references/tool-notes.md`. Routine runs should not load it; read only the relevant section when
adding an event, resolving a failure, or changing the reducer.
