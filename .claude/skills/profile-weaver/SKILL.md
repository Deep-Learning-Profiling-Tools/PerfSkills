---
name: profile-weaver
description: Route an HPC performance question to the cheapest capable profiler, then produce a measured, correctness-checked optimization. Use for profiling, optimization, regressions, profiler selection, and evidence-backed rewrites.
---

# ProfileWeaver

Find a correct improvement from measured evidence. The agent chooses and edits candidates;
the controller collects evidence and applies timing, correctness and attribution gates.
Unknown measurements stay null. A profiler cannot prove mechanisms it does not observe.

## Routine optimization

Run commands from the repository root through this public entry point:

```bash
python3 shared/scripts/experiment.py start --workload <directory> --iterations 10 --repairs 7
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json prepare-batch --candidate a:baseline:<mechanism>
# edit the returned candidate source workspace
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json evaluate a
# use :current: to inherit the measured leader, or explicitly name another parent
```

Wait on running command sessions for up to 60 seconds per poll; avoid repeated one-second polls.

A manifest's `selection_objective` can declare a kernel-only goal. Override it with
`--objective kernel --kernel REGEX`. This mode compares activity sums from one provider/scope,
labels the profiled basis and retains unprofiled application collateral. Goal corrections use
`objective --scope kernel --kernel REGEX`. Renames use `prepare --target-kernel REGEX`.
Read the returned objective and baseline before choosing an edit. [Selection and inspection](references/skill-runtime.md).

Read the returned brief and inline candidate evidence. Resolve one connected source dependency
at a time, using the compact index and bounded source pages:

```bash
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json inspect --section source
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json inspect --section source --target target-01
# --candidate ID selects that snapshot; --offset N retrieves the next page
```

Do not dump the complete source-targets JSON or read all sibling implementations in advance.
`existing-variant` slices are structural references, not measurements. Follow named caller,
callee, producer, transfer and consumer edges when needed; a coherent candidate may span files.
Reuse source content IDs with `--known ID`. Packets include changed lines and build errors.
Benchmark inspection is compact and paginated. Use `--candidate ID --group detail` when needed.
Literal disabled-condition observations describe source, not execution or performance. Account
for deliberately disabled paths when interpreting later evidence; the controller never decides
that they must be enabled or supplies a benchmark recipe.
Check the application's reported configuration against conditional source paths before editing.
An improvement to an inactive branch cannot explain a measured gain. `inspect --section harness`
exposes the command, metric, reported categorical settings and baseline qualification.

During an optimization journey, edit only returned candidate sources. Do not read framework
Python, schemas, plans/state, raw traces, full logs, child skill files or unrelated source.
Do not author orchestration/reporting scripts or repair framework code during the journey.
Use the controller's named inspections and compact failure diagnostics. Harness maintenance is
a separate task, not a workaround inside a failing optimization run.

## Connect the cost to the code

Compare removing repeated work or traffic with improving its execution. Follow bounded
producer, transfer and consumer slices: setup, temporary arrays, repeated preparation and
neighboring outputs may explain more cost than a local instruction change. Existing alternatives
are useful comparisons, never prescribed winners. Source reuse and dependence ordering must
justify a proposed fusion, precomputation or representation change.
A `PHASE` line compares related work within its measured scope. Inspect the named phase
source first when an application timer or existing timeline answers the question. GPU-idle
time alone leaves its cause open. Request the named host timeline capabilities when needed,
and use kernel counters for a separate question about execution within a hotspot.
Before using an unfamiliar API from a prebuilt dependency, use
`inspect --section harness --group link --candidate ID --target SYMBOL` to check its
definition in the linked libraries. The response names the build snapshot and include binding.
After a gain, use current timing to identify the remaining cost. After a regression, compare the
parent and the retained leader; an improvement over a slow parent can still lose to baseline.
A short plateau does not imply that the remaining transformations have no value.

## Spend evidence where it changes a decision

Start with the existing application/CUDA-event timer, correctness and static binary resources.
New adaptive baselines choose a capable activity/context provider; CUPTI can attribute existing
NVTX ranges. Missing ranges, record loss, conflicting injection or unsupported scope require a
capable fallback. Baseline NCU is on demand. Existing saved plans and `start --policy legacy`
retain their contracts. Never insert timer calls or change numerical precision to claim timing.
Baseline correctness/timing is emitted immediately in live progress, before profiling finishes.
Adaptive baseline captures have a deadline derived from measured process time (30 seconds minimum,
three times baseline wall time, capped by the application timeout); `baseline_capture_timeout_s`
can override it. A timed-out capture fails explicitly with retained logs. Do not report a stalled
capture as normal profiling overhead. Declared `profiling.tool_paths` bind preflight, build and capture.

New adaptive screens compare editing now with buying a missing observation first. They price
provider wall time and compact evidence tokens against recent correct incumbent gains and edit
costs. When the packet says `compare-source-hypotheses`, compare two plausible transformations
using existing evidence (including a producer or consumer when relevant) before buying a replay.
After a regression, compare a local change with a connected producer/consumer hypothesis using
the source handles. Useful late gains can follow plateaus; keep a fixed requested iteration budget. Name the missing observable before buying another replay. Recent baseline
counters remain useful when that baseline is unchanged; a failed candidate does not invalidate them.
Explicit questions and changed execution identities can still justify a new capable provider.

Acquisition shares one deadline across routing, setup, capture, export and reduction; it is
based on baseline wall time and the application limit. `profiling.acquisition_budget_s` overrides
it. Providers do not receive fresh budgets at each stage. Targeted NCU uses preflight-verified
sections; full NCU is explicit. Automatic optional providers need positive capability probes.
Completed measurements are checkpointed before diagnostics, allowing unchanged-input recovery
without more benchmark runs. Failed profiling leaves attribution unresolved. See the engineering
reference for deadline details; ordinary optimization needs only the returned verdict.

| Missing fact | Example request or capable surface |
|---|---|
| Timed kernels, copies, existing ROI | CUPTI activity; NSYS for richer context |
| Host waits, launch gaps, overlap, MPI phases | `evaluate ID --need sync` / NSYS |
| Literal sampled stalls and PCs | `evaluate ID --need gpu-pc --need instruction-stalls` |
| Occupancy for a launch-shape decision | `evidence ID --need gpu-occupancy` / targeted NCU |
| Cache traffic and memory throughput | `evidence ID --need memory-bandwidth` / targeted NCU |
| Roofline bound for a named kernel | `evidence ID --need gpu-roofline` / targeted NCU Roofline section |
| Calling context | `evaluate ID --need call-context` / probed HPCToolkit |
| Causal stall attribution or redundant values | GPA or value adapter only with positive compatibility/artifact evidence |

Repeat `--need` for required capabilities. `--provider` explicitly requests a capable provider;
`--evidence timeline|kernel` requests a broader surface on `evaluate` or profile-only `evidence`.
`gpu-kernel-time` aliases `gpu-hotspot`. `--ncu ID` forces full counters.
CUPTI PC sampling is not GPA causal advice. Static registers are not achieved occupancy.
Timing profiles cannot establish cache misses. An unavailable provider stays explicit.
The controller compares capability coverage first, measured acquisition cost next, configured
priors when uncalibrated. Roofline/Amdahl are historical opportunity bounds, not predictions.
Final winner evidence still uses the configured independent profiling gates.

New adaptive plans learn profiler recipes within an observed environment binding. Actual capability
coverage, new evidence and wall cost revise provider/question preferences; repeated failures roll
back a recipe. Recent ancestor evidence is reused. Timing-only evidence does not resolve kernel
internals. The compact packet names learned decisions; full experience stays backend-only.
Repeated validated invocation parameters can be reused with `evidence ID --recipe`; environment
drift invalidates the binding and reroutes the question. Request only evidence needed for a decision.
This learning never authors application edits or adds benchmark repetitions. Existing plans retain
their saved policy. When a measured fact motivates an edit, pass its `cite` ID with
`prepare-batch --because ID`. Optionally describe a competing next edit with
`--consider 'capability=rewrite'`. Exact iteration counts stay fixed. [Skill runtime](references/skill-runtime.md).

For a new question about an already measured snapshot, use
`experiment.py <plan> evidence current --need <capability>` (or name a candidate or `baseline`).
This collects profiling evidence without another benchmark evaluation. Check existing evidence
first. Automatic plateau diagnostics target the retained measured leader, with its snapshot
named explicitly, rather than spending the capture on the latest regression.

Each evaluation returns at most 640 tokens of candidate evidence. Request only a named missing
group (`inspect --section evidence --candidate ID --group timing|resources|stalls|contexts|locations|findings`).
Packets preserve current timing, changed regions and a source-inspection handle. Reference
counters name their own snapshot. `evaluation_passed` means the measurement completed;
`candidate_result` states improvement, regression or unresolved ranking. Screen `accepted`
requires an observed improvement under the bounded policy. The full artifact remains on disk.
Never attach baseline timings to current counters. Application objectives rank unprofiled
measurements. Explicit kernel objectives report the consistent provider and profiled timing basis. Distinguish application ROI, lookup-only time and process wall.

## Measurement and stopping

A kernel objective may declare `selection_objective.collateral="profiled"`. With a capable
CUPTI baseline, one activity execution supplies correctness, primary kernel timing and explicitly
profiled application collateral. Application objectives and `collateral="unprofiled"` retain
separate unprofiled measurements. The manifest determines this requirement.

New adaptive runs otherwise take **one unprofiled benchmark execution per candidate**.
A manifest can set `sampling.benchmark_max_runs` to 2 when a second execution is worth the cost.
Count process executions, not baseline/candidate pairs.
There is no automatic warmup. The baseline timing comes from its existing correctness execution
and is reused. The two-execution policy can spend one execution recovering a missing baseline reference.
Every candidate execution also runs the correctness oracle.
Unchanged measured snapshots are cached rather than benchmarked again.
When an unprofiled reference is required, if the same application metric from required profiling is substantially better
than the baseline reference, check the baseline once more (maximum two unprofiled baseline runs).
Use the conservative unprofiled reference; instrumented metrics never rank candidates. Remaining
disagreement stays explicit. This check adds no warmup or repeated final campaign.

**There is no automatic final benchmark campaign.** Final selection reuses the existing timings
and valid profiling evidence, acquiring missing attribution separately when necessary. No default
final NCU audit is forced in this execution policy. Profiling executions are accounted separately
from unprofiled benchmarks. A clear gain is a measured result, not statistically confirmed:
confidence intervals stay null and unique-winner/significance claims remain inconclusive.
Saved legacy plans retain their recorded policy; new starts use this bounded execution policy.

**Stop when `campaign_complete=true` and report the supplied result.**
The complete ledger, all changed lines/functions, and exact patches are already recorded in
`ITERATIONS.md`, `iteration-ledger.json`, and each candidate’s `source-changes.json`.
Use their links in the final reply. Reconstructing diffs, reopening source for line numbers,
and recounting tokens add no reporting evidence. `report` retrieves the cached ledger if needed. The last
evaluation already selects the measured result. Do not start another confirmation merely to
resolve a close candidate ranking. Plain `confirm ID` reuses verified evidence; `confirm --fresh`
is for a specific unresolved attribution/collateral question or an explicitly requested new study.
A failed challenge preserves the published verification and saves its own diagnostic result.
If independent timing passed but profiling/reporting failed, `confirm ID --retry-evidence`
verifies unchanged inputs and reuses those samples; it does not repeat paired benchmarks.

`check ID --build-only` compiles without correctness; `check ID` validates once;
`check ID --observe` shares validation with activity collection. At most two linked unmeasured
candidates may be checked before paired evaluation. Checks never establish a speedup.

Report the exact measured metric, oracle, final sample count, source changes and material limits.
No unique-winner or whole-process improvement claim unless its evidence resolves it. Preserve
all candidates, including regressions, and the best verified source workspace.

Controller costs are saved automatically to `resource-summary.json`. Session tokens come from
supplied client counters and remain null when unavailable. `tokens.py` is an optional audit;
normal completion needs no token command or skill inventory.
For harness engineering only, read [adaptive acquisition](references/adaptive-acquisition.md)
and [native providers](references/native-providers.md). Routine optimizers need neither.
