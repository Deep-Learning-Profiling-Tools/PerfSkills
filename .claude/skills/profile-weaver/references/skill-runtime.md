# Environment-bound adaptive profiler workflows

This implementation adapts ideas from [SkVM, §§4–5](https://arxiv.org/pdf/2604.03088):
environment binding, solidifying repeated code, adaptive recompilation after systematic failures,
and deriving execution dependencies. The application here is profiler acquisition. SkVM's
reported performance improvements are not measurements of ProfileWeaver.

## What evolves

New adaptive plans enable `profiling.skill_runtime.enabled`; old plans and legacy starts do not
silently opt in. A workload may disable it through its profiling settings. Sampling remains one
candidate execution, at most two, and finalization does not start a benchmark campaign.

The runtime revises declarative question/provider preferences, not arbitrary Python or application
source. Its experience cache is local to the checkout, under `runs/_cache/skill-runtime/`.
Every adapter must still pass current positive capability checks. A history entry never installs
a tool, invents support, or turns a failed capture into evidence.

| Mechanism | Current implementation | Work removed from the agent |
|---|---|---|
| Environment binding | Observed device/compiler/profiler versions, tool-file stamps, inherited/declared settings, tracked adapter implementation hashes | Reconstructing which installation and environment a recipe expects |
| Code solidification | Typed invocation parameters in `compiled-skill.json`; reuse through `evidence ID --recipe` after two consecutive capable acquisitions | Repeated command assembly or benchmark-script authoring |
| Adaptive recompilation | Revise question/provider rankings from capability coverage, evidence novelty and acquisition wall time; back off after two consecutive underdelivering captures | Retrying the same broken or uninformative recipe across runs |
| Dependency extraction | Derive edges from typed input/output contracts; mark captures as requiring exclusive GPU use | Reasoning repeatedly about capture/reduction ordering |

The existing preflight and typed capture/reduction adapters are reused. This is not a second
script-generation system. Dependencies are described explicitly; the runtime does not infer an
arbitrary program's dependencies from natural language. Execution remains sequential through the
existing adapters, even when the emitted graph shows independent CPU work.

## Closed feedback loop

1. Read measured workload properties. A dominant kernel and visible device-idle time pose
   different questions. No benchmark name or winning source optimization enters the policy key.
2. Determine the missing view. Examples include instruction stalls, throughput/occupancy,
   host synchronization, call context or positively supported causal/value evidence.
3. Reuse recent relevant ancestor evidence. Otherwise rank unresolved questions using comparable
   historical evidence yield/cost; use disclosed priors when no measurements exist.
4. Check cached positive optional PC/native capability probes for automatic routing; an explicit
   provider request may establish an uncached capability. Compare validated alternatives using
   coverage, novelty and question-specific measured seconds. Unexplored tools remain available for distinctive capabilities. The runtime
   does not run calibration captures solely to train this policy.
5. Record the actual returned capabilities, acquisition wall time and diagnostic signature.
   Requested capabilities are not copied into the observed set. Cached/reused captures and
   duplicate acquisition records do not train another observation.
6. Add weak downstream credit only for an immediately linked, correct child with a usable
   timing screen. Unstable/resolution-limited results do not train that credit. This is an
   observational association, with at most a 10% scheduling influence, not causal attribution.
7. Validate a recipe after two consecutive capable acquisitions. Two consecutive failures or
   incomplete answers quarantine it for automatic selection; prefer a validated alternative.
   Explicit provider requests retain authority. Environment drift creates a new binding.

Coverage and novelty are scheduling heuristics. They are not a measured probability of finding
the best optimization. Provider quality can be compared without first exercising every installed
profiler; untested alternatives stay explicit. A capture's status, literal observations and
unanswered questions remain separate.

## Bounded acquisition and recovery

Learning refines a justified evidence request; it does not turn a timing/static-only decision
into profiling just because a dominant kernel lacks counters. Unstable or resolution-limited
screens remain provisional. Known provider costs must fit the remaining acquisition budget.

One cumulative deadline covers routing, probes, capture, exports and reduction. Its default is
`max(30, 3 * baseline_process_wall_s)`, capped by the application timeout. An explicit
`profiling.acquisition_budget_s` replaces that default. Nested acquisition does not reset the
clock. Automatic optional probes never hide cold SDK builds or device-enablement experiments.
Focused NCU sections must answer the requested capabilities and be verified by preflight;
missing sections leave the question unresolved rather than expanding to full collection.
Source/SASS export is reserved for a source question or explicit full replay.

Correct timing and the measured leader are saved before optional profiling. Timeouts terminate
the capture process tree and retain live diagnostics. Profile failures do not discard timing or
claim that the mechanism was verified. Unchanged interrupted evaluations recover from their
campaign artifact after checking its source/input fingerprint, without another benchmark run.

## Mixed providers and compact evidence

A CUPTI baseline may be followed by NSYS context or a targeted NCU replay. These supply different
views. Changed providers produce observations for the current source snapshot, with profiled
speedup left null. A candidate NCU replay can proceed without forcing a baseline NCU replay.
Unprofiled application timing alone ranks candidates. Legacy comparability checks are retained.

The 320-token brief and 640-token candidate packet remain bounded. Learned decisions add a
reason and revision; full scores, environment bindings and dependency graphs stay on disk.
`inspect --section harness` exposes the binding/compiled-recipe disposition when needed.

## Choosing between another edit and another profile

New adaptive plans record `profiling.skill_runtime.decision_policy=marginal-value-v1`.
At a plateau the controller compares feasible question/provider combinations in the same
decision. A visible GPU idle interval opens synchronization and calling-context questions.
A busy hotspot opens throughput, stall and source-attribution questions. Two linked edits
whose observed upper speedup bounds both miss the incumbent can also trigger this comparison.
A single noisy sample supplies no mechanism evidence.

The score uses log improvement over the best correct version of the declared objective.
For a candidate observation, the controller compares the estimated gain of a guided edit
with the recent gain of editing immediately. It charges extra profiling seconds in units of
recent build-and-measure time and charges a quarter of the practical gain threshold per
640 packet tokens. These weights and cold-start expectations are policy priors. Local
same-question wall measurements take precedence over environment-matched observations
adjusted for application startup. Unmeasured tools start with a labeled cost prior.
An optional `profiling.skill_runtime.campaign_wall_budget_s` reserves expected time for the
remaining required edits before admitting optional captures. Mandatory checks retain their
own deadlines.

The controller learns separately from requested capabilities, literal facts delivered in
the packet, and subsequent correct incumbent improvements. It splits observational credit
across recent evidence on the selected branch. The optional `optimizer_id` isolates outcome
history across optimizer versions. Packet tokens measure the compact response; total agent
tokens require session accounting. An observation with negative net value yields an edit
or a comparison of alternative source transformations using existing facts.

`candidates/<id>/acquisition-decision.json` records the competing actions and cost bases.
`observations/` in that candidate directory preserves normalized captures independently of
profiler scratch files. Matching evidence-only requests can reuse observed facts after
validating the measured inputs, environment, target and scope. The oracle and the one/two
unprofiled-execution contract still govern acceptance.

## Conditional evidence acquisition

New plans use `budgeted-lookahead-v3`. Existing plans retain their policy version.
The v3 model discounts old progress after unsuccessful edits and uses the measured spread of
correct gains to size an uncertain rewrite opportunity. It compares supporting, contradictory
and empty observations over the remaining edit slots. Repeated correct but noisy misses can
open a costed diagnostic question. Noise itself never proves a kernel mechanism.
A matching measured timing fraction supplies an Amdahl removal bound when counters are absent.
Existing baseline counter capabilities enter reuse decisions. Cost limits, positive tool probes,
current target identity and actual delivered capabilities continue to govern acquisitions.
These scheduling priors require campaign validation for speedup, wall time and full session tokens.


New adaptive plans compare an immediate edit with buying evidence over at most three remaining
edit slots. A supporting observation, a contradictory one, and an empty capture can lead to
different next edits. A successful edit opens a dependent refinement. The cold diagnostic
reliability prior is 0.8. These probabilities and utilities are scheduling assumptions.
Observed coverage, delivered evidence, cited outcomes, costs, and remaining time adjust the choice.

The agent can link a displayed fact to its edit and propose a competing hypothesis in one command.

```sh
python3 shared/scripts/experiment.py "$plan" prepare-batch --candidate b:a:tile-rows \
  --because "$fact_id" --consider 'instruction-stalls=shorten-dependency-chain'
python3 shared/scripts/experiment.py "$plan" inspect --section fact --target "$fact_id"
python3 shared/scripts/experiment.py "$plan" inspect --section decision --candidate b
```

A receipt identifies the snapshot, target, provider, scope and literal value that reached the
optimizer. Cited edits earn credit from correct, stable incumbent-relative outcomes. Explicit
links are optional. Both the focused and contextual packet use measured values. Their relative
utility is learned within a provider, workload context, objective and optimizer identity.
Supply `start --optimizer-id MODEL_OR_AGENT_VERSION` to distinguish optimizers. A historical
receipt can lower the value of recapture when mapped files survive an edit. Changing a mapped
producer, header, build input, or unknown dependency expires that scheduling prior. Current
verification still requires the matching measured snapshot.

`start --iterations N` requests an exact study. `--up-to-iterations N` permits an earlier
finish when observed costs exhaust a declared budget or when the expected gain falls below an
explicit `profiling.skill_runtime.minimum_expected_log_gain` after stable screens. Finals reuse the measured
samples. `campaign_wall_budget_s` reserves time for edits. The optional `resources.budgets`
keys are `cpu_s`, `allocated_gpu_h`, `energy_j` and `session_tokens`. A missing meter or cost
forecast makes discretionary capture ineligible under the corresponding cap. Discovery and
required verification have their own capability contract.

## Selection scope and compact inspection

A workload manifest can declare its selection facts independently of acquisition policy.

```json
"selection_objective": {
  "scope": "kernel",
  "kernel_regex": "^(operation_baseline|operation_fused)[(]"
}
```

An optional `comparison_provider` requests a baseline/winner comparison with a reference
profiler after selection. It reuses valid captures and performs no extra unprofiled benchmarks.
For example, a historical NSYS kernel comparison can use `"comparison_provider": "pw-nsight-systems"`
while the primary timing provider is acquired dynamically. A goal correction may supply the same
request with `objective --comparison-provider PROVIDER`.

Omit `provider` to choose a capable baseline timing provider dynamically. All declared kernels
are summed, so the selector must cover the complete operation after a transformation.

`start --objective kernel --kernel REGEX [--timing-provider PROVIDER]` records an explicit
kernel activity objective. All matched kernel names are summed within the same measured scope.
Comparisons use one provider. NCU replay durations never enter this ranking. The declared
application metric remains available as collateral with its original unprofiled samples.

`objective --scope kernel --kernel REGEX` revises selection through the public CLI. It preserves
the original sampling contract, records earlier published selections, validates measured inputs
and re-ranks existing captures. Missing observations remain unresolved. The selector may be
overridden for a renamed candidate using `--candidate ID --kernel REGEX`, and new descendants
inherit that mapping. `prepare --parent current`, status and bounded finalization use the same
objective. A new explicit request is needed to change the comparable timing provider.

Source pages have a token bound and stable content IDs. Re-reading with `--known ID` can return
a compact unchanged receipt. Source indices identify duplicate emitted source windows.
Evaluation packets carry literal changed line ranges from the saved parent patch. Build failures
carry a bounded excerpt from the named compiler stderr. Routine benchmark inspection is paginated
and includes measured values, correctness and quality. `--candidate ID --group detail` retains
access to the underlying sample record.

## Avoid repeated execution and reporting work

`selection_objective.collateral` defaults to `unprofiled`. A kernel-only workload can explicitly
select `profiled` when its application figures are collateral. On the CUPTI timing path, the
same execution validates the configured oracle, measures the primary kernel and records profiled
application and process wall time. It performs zero separate unprofiled candidate executions.
The baseline uses the corresponding saved CUPTI observations. Every collateral field records
its profiled basis. Source/input drift, lost activity or a failed oracle prevents publication.
Other timing providers retain the bounded unprofiled path.

Required primary activity also supplies a graph signature. When a recent capable context capture
exists, the current graph has identical kernel call counts, launches and copy volume, and the
collateral sample has not worsened, the policy can reuse historical context. It labels that
context as historical. Graph changes, unknown fields, worsened collateral and explicit provider
requests retain acquisition. Exact same-snapshot capability requests reuse matching captures.
Equivalent profiler spellings are joined only through one unique compiled kernel symbol.

Each successful measurement saves `source-changes.json` with complete literal changes and
parent/candidate ranges. Finalization renders `ITERATIONS.md` and `iteration-ledger.json` from
those saved records. The completion capsule carries the winner, requested reference comparison,
correctness and report links. `report` serves the cached result without a new comparison, diff,
profiler or skill inventory. Source-line localization questions can compete with broad counters
when their providers have demonstrated capability and the added evidence can justify its cost.

`resource-summary.json` records controller costs automatically. Optional definition audits only
count explicitly loaded definitions by default. A full inventory requires `tokens.py skills` or
`--inventory`. Codex usage requires a bound session ID/path or caller-supplied counters. The
reader uses the latest cumulative record once, subtracts cached input for the stated uncached
convention, and counts reasoning inside output. It never sums cumulative records or unrelated
goals. Unavailable session totals remain null.

## Validation boundary

Unit tests exercise changing provider/question choices, invalidated bindings, recipe promotion,
rollback, noisy-feedback exclusion, mixed-provider collection and preserved execution budgets.
Archived-run replay demonstrates a changed acquisition decision without executing a benchmark.
These checks do not establish faster campaigns, fewer LLM tokens, or better optimization wins.
A fresh controlled campaign is still needed to measure those outcomes. It must retain the
one/two-execution cap and must not silently add a repeated finalist study.
