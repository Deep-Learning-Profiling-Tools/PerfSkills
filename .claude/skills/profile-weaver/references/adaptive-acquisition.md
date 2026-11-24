# Adaptive acquisition (maintainer design)

The optimization agent owns source edits and search strategy. The controller spends measurement
effort on decisions whose missing evidence can change the next edit.

| Surface | Observable evidence | Invocation |
|---|---|---|
| Built CUDA binary | Per-kernel registers, stack, local/shared bytes | `cuobjdump --dump-resource-usage`; no application execution or flag changes |
| CUPTI concurrent activity | GPU kernel/copy durations and counts, observed NVTX region attribution and activity union; explicit loss/error counts | One execution without replay; baseline discovery, hotspot refresh, or shared with `check --observe` validation |
| CUPTI current PC sampling | Literal sampled-warp stall metrics and PC offsets, reduced for an already timed kernel | Device/API probe and cached SDK collector/decoder; separate cost history; no GPA causal blame or kernel timing |
| Unprofiled application/CUDA-event metric | ROI, correctness, process wall time | One paired screen; second near a tie/incumbent or timer resolution; automatic unranked warmup only for short or unknown regions |
| CUDA/NVTX timeline | Kernels, launches, transfers, synchronization, overlap | Baseline, large gain, ROI/wall disagreement, aged evidence plus runtime movement, explicit question, final winner |
| Filtered counters | Transactions, scheduling, occupancy, compute/stalls | Baseline, resource-growth trigger, explicit kernel question, final winner |

Existing verified section tiers keep NCU focused on the measured diagnosis. Final counters use
the full set for one filtered kernel. Static registers cannot establish occupancy or spill traffic;
a throughput bound cannot establish a candidate speedup. Missing evidence stays unknown.
Baseline Roofline and Amdahl constrain hypotheses, not acceptance.
Candidate acquisition exposes their priority/cost score; small or near-saturated historical
targets get a fresh timing map first. Age alone never starts a periodic profile. Questions
follow the observed memory/register/divergence family, with capability-qualified alternatives;
value evidence is a separate branch. See [native providers](native-providers.md) for the score,
current PC sampling and GPA architecture checks. New adaptive baselines are capability-first:
application correctness/timing and binary resources, then the cheapest capable activity/context
provider. Baseline counters are on demand. Missing ROI/context capability requires escalation;
it never permits assigning setup work to the primary region. Final timing retains the baseline
provider for comparability; the configured final NCU audit remains separate. Legacy plans retain
their startup/final contracts.

One or two pairs only rank candidates. Independent final pairs, correctness, attribution,
whole-process gates, and measured target reduction decide acceptance. Adaptive screening cannot
use the legacy zero-run finalizer. Final replay occurs once per surface after statistical gates
pass. Inconclusive evidence keeps the run inconclusive.

`check --build-only` builds without launching and leaves correctness unknown. `check` validates
once. Neither displaces the measured best. Two linked unmeasured candidates can be checked
before evaluation.

New `start` defaults to adaptive; `--policy legacy` preserves previous behavior. Resuming old runs
keeps their policy. Capsules contain acquisition reasons and resource deltas. The text brief
retains its existing token cap.

CUDA events cannot provide compile-time timing. Existing event/stdout metrics are supported;
automatic timer insertion would change application instrumentation and is outside this change.
For a workload that reports a CUDA-event interval, configure the output `metric` and
`kernel_timing.source = "cuda-event"` in its manifest. The optional `kernel_timing.detail`
names the region. The start response includes `baseline_observation.observations.cuda_event`
beside CUPTI kernel time. Later CUPTI packets carry the same-capture event time in
`roi.instrumented_sample`, alongside the benchmark result in `roi` and CUPTI kernel time
in `observations`. The event time read during profiling is diagnostic. Both use the
existing packet and execution budget.
The CUPTI collector uses concurrent activity records, separate per-process outputs, dropped-record
checks, and validated normalized profiles. NVTX markers establish observed region boundaries
for the single-process ROI path; interval clipping/exclusion and union are explicit. Missing
ranges, incomplete/truncated records or conflicting NVTX injection require a capable fallback.
Timing questions about the whole process use aggregate records. ROI questions retain only the
requested range and its exclusion markers. Separate command oracles run without the collector.
It cannot establish host synchronization or replace the full timeline. Profiling runs never rank candidates.
HPCToolkit/GPA and artifact readers can be declared through `profiling.adapters`: each adapter
requires explicit `capabilities`, capability `probes` (argv plus expected regex), and capture/reduce
`stages` that emit `{profile_output}` in the common schema. Independent tools do not acquire fake
deltas against NSYS. Missing adapters stay blocked; no unsupported substitution occurs.

`evaluate --need CAPABILITY` routes the precise question. Built-in adapters are available for
CUPTI, NSYS, and NCU; HPCToolkit/GPA adapters require a validated local toolchain and reducer.
The router compares full capability coverage first, then measured wall costs among calibrated
contenders. Configured priors apply when none has a measurement. An unknown equivalent provider
does not erase a measured cheap choice; distinctive questions or explicit provider requests can
calibrate it. Measured seconds and ordinal priors are never mixed.

HPCToolkit/GPA now have built-in adapters and capability probes, rather than requiring workload-
specific stages. See [native providers](native-providers.md) for invocation and limits. The
generic adapter interface remains available for other toolchains and artifact formats.

Checks: `python3 -m unittest discover -s shared/tests -v`.
Live study: `runs/20261113-adaptive-study`.

## Screening confidence and source provenance

Printed decimal granularity is read from the declared metric's matched output. It describes
reporting precision, not timer accuracy. A declared `sampling.metric_resolution` overrides it;
integer counters and process wall timers have no inferred quantum. A coarse measurement can
spend the second screen pair even when the apparent gain is large. Observed spread and output
precision form a conservative screening envelope explicitly labeled **not a confidence interval**.
An unstable screen stays provisional. Instrumented application output can flag disagreement but
never supplies a ranking score or substitutes for an unprofiled run.

At budget exhaustion, the default new adaptive policy confirms one screened leader against
baseline with 3 independent pairs, increasing only when gates remain unresolved, up to 5.
`sampling.adaptive_final_min_pairs`, `adaptive_final_max_pairs` and `adaptive_finalists` explicitly
request a stronger budget or two finalists. Small-sample bootstrap intervals are conditional on
the observed samples, not guaranteed coverage or proof of unique optimality. Noisy results can
remain inconclusive at the cap. A correct overlapping contender can join when two finalists
are requested; both share one baseline campaign. A timing
tie can verify improvement over baseline when finalists are practically equivalent; the final
selection explicitly says it is not a unique winner. An unresolved comparison stays inconclusive.
Printed precision also bounds final acceptance: improvement over baseline must survive the
output quantum. Finalists unresolved at that precision are labeled indistinguishable, not
uniquely fastest or proven practically equivalent.
Screens still evaluate one candidate at a time. Legacy sampling and final selection stay intact.

New adaptive starts can improve representation of an existing stdout metric through
`metric_output.py` and a cached `metric_output.c` library. A literal prefix derived from the
declared regex must qualify; only a single numeric floating printf conversion is expanded.
Arguments, nonmetric output and printf return count are preserved; dynamic precision, hex floats,
multiple floating conversions, `%n`, existing preloads and unsupported platforms are not changed.
No application source, timer, numerical precision or compiler flags are changed. The environment
and library are fingerprinted and `measurement-output.json` records support; timer accuracy stays
unknown. `metric.output_precision=false` disables it. This does not recover a value already
rounded by the application, or support every output API. Persistent coarse output is diagnosed
at minimum final pairs, rather than spending more samples to recover discarded digits.

Checkpoint output has a 640-token budget; full resources remain in compiler/check artifacts.
Check/build/validation command wall cost is recorded even on failure. Cached profile reductions
do not calibrate fresh acquisition cost. Deferred evidence follows the candidate's own ancestor
chain, not an unrelated global-best branch. Simple kernel names may join different const-qualifier
spellings only when exactly one matching kernel is compiled; overload ambiguity remains unknown.
ROI/wall disagreement requires repeatable material wall regression (or a large single effect),
with stricter handling when the workload declares a strict collateral gate.

A successful provider with the requested `gpu-hotspot` capability refreshes the evidence anchor,
including CUPTI and configured adapters. Two actual non-improving screens are required before
an automatic plateau counter request; a fresh win followed by one regression is not a plateau.
Static resources still cannot establish a dynamic bottleneck.

When a manifest declares an application ROI, primary hotspot refreshes also require
`roi-attribution`. Whole-process activity stays collateral evidence: a setup kernel cannot
silently replace the ROI target or become the inherited target of a later PC/counter capture.
CUPTI qualifies only after actual complete NVTX-region records are reduced; whole-process-only
activity never supplies this capability.
A newly compiled kernel identity requests a timing map before automatic internal diagnostics;
compilation alone does not prove it executed. Explicit agent questions remain available.
Automatic candidate warmup is skipped when the baseline profiled region is at least one second;
an explicit `sampling.screen_warmup` is preserved. This avoids an unconditional extra execution
for long workloads while retaining the observed first-use guard for short ones.

NCU counters join timeline timings only for the same phase, candidate, and exact kernel symbol.
Otherwise the capsule exposes the captured symbol and leaves current ROI time, wall fraction,
and Amdahl fraction null. The agent brief names the candidate snapshot and its active source
slices, with baseline anchors separately labeled. Matching binary evidence can still be reused;
another candidate's application times are not copied into it.

New adaptive starts record declared source hashes, resolved build commands, compiler substitution,
tool paths, GPU UUID/driver inventory, and a bounded environment record in
`startup-provenance.json`. This is startup evidence, not proof of identical clocks or runtime
load. An optional `start --baseline-reference <source-directory>` checks declared source,
including unused headers, before building. It honors manifest copy exclusions and never resets
or deletes source. This guard cannot reconstruct a missing historical source snapshot.

Saved-artifact integration replay (maintainer use, no benchmark/profile execution):
`python3 shared/tests/replay_refinement.py --run <recorded-run> --output <new-directory>`.
It replays reductions, schema validation, source location, timing diagnostics, and bounded briefs.
It tests evidence handling, not campaign time or achieved optimization quality; those still need
a fresh controlled ten-iteration run with fixed source, environment, and optimizer identity.

## Profile-only questions and final recovery

`experiment.py <plan> evidence current --need CAPABILITY` profiles the measured leader without
repeating paired benchmarks; an explicit candidate or `baseline` is also supported. The current
source/input fingerprint must still match the measured snapshot. Automatic plateau diagnostics
profile the retained leader when the current trial is a regression; the trial capsule names
that reference snapshot explicitly. Profiler acquisition never authors candidate edits.

`confirm ID --retry-evidence` reuses a matching passing independent final campaign only under
its unchanged fingerprint and retries profiling/reporting. It cannot repair failed statistics.
New adaptive final output is terminal (`campaign_complete=true`); do not run a second campaign
just to distinguish practically close candidates. An explicitly requested failed challenge
archives its artifacts and preserves the prior verified state/report/profile files.

The source inspector returns a six-entry frontier or one page of at most 80 emitted source lines.
Literal disabled conditionals introduced relative to baseline are retained as source observations
across descendants; they are not generated mechanisms, recommended edits or execution evidence.

## Current execution cap (single-or-two-v3)

This supersedes the earlier new-run 3–5 pair default described above. New starts take one
unprofiled candidate execution. Set `sampling.benchmark_max_runs` to 2 to permit a second
execution for uncertain timing. Automatic
warmups and final paired campaigns are disabled. Baseline correctness supplies its timing
reference once. The measurement verdict reuses these samples and names uncertainty explicitly;
no confidence interval or statistical significance is invented. Final attribution reuses a
valid timed profile or requests a capable lightweight capture, not an unconditional NCU audit.
Already recorded plans retain their contracts.

Adaptive CMake workloads using the standard build directory template use `ccache` when
available. Each snapshot has a sibling build directory with stable relative source paths,
and a campaign shares cached compiler outputs. The cache verifies compiler and input content.
