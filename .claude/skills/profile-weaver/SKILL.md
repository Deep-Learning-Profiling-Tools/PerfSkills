---
name: profile-weaver
description: Route an HPC performance question to the cheapest capable profiler, then produce a measured, correctness-checked optimization. Use for profiling, optimization, regressions, profiler selection, and evidence-backed rewrites.
---

# ProfileWeaver

Use: `preflight once → route → timeline/locate → baseline NCU → [benchmark → timeline/diff → NCU capture-or-reuse → frontier] → confirm → fresh final reprofile`.

Record the exact build, run, input, environment, oracle, metric, and baseline fingerprint. Run
`preflight.py` once for compiler/device/profiler/build/launch checks; reuse its cache. Route to
the cheapest capable child and run profiling through `harness.py`. Consume only
`agent-brief.txt`, per-iteration delta capsules, and their named source slices. Resolve one named
dependency at a time only when the slice cannot support the decision.

For NVIDIA GPU work, route `pw-nsight-systems` first to rank absolute kernel cost and locate one
source target. Escalate that kernel to `pw-nsight-compute` when the next decision needs stalls,
scheduler eligibility, register/resource limits, instruction/control-flow, cache/traffic,
throughput, or roofline evidence. `--section all` resolves to the installed NCU full set and its
wall time is recorded. The ordinary brief remains at most 320 tokens; an NCU-enriched brief is at
most 480 tokens and names bounded drill-down groups. Full counters never enter agent context.

Create and initialize one `experiment-plan.json`. It declares the routed child's tool-neutral
iteration stages. The controller owns candidate copies, inherited contracts, patches, internal
plans, logs, sampling, candidate profiling, normalized diffs, source ranges, state, confirmation,
and finalization.

```bash
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json init
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json prepare-batch \
  --candidate a:baseline:<mechanism> --candidate b:baseline:<mechanism>
# edit only the returned candidate source ranges
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json evaluate a b
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json confirm a [b]
```

Screen up to three orthogonal structural mechanisms per batch; change one variable. Every correct
candidate is profiled once with the routed timing child under the baseline scope. The controller keeps
the capture and full diff on disk and returns a bounded capsule: profile wall/GPU movement,
declared-target movement, largest measured gain/regression, and the next hotspot's source slice.
Additional batches and measured-parent inheritance preserve search breadth. Confirmation never
rescreens or reprofiles a screened candidate; it returns the cached delta and stops from five
pairs when all gates resolve. The accepted winner receives a fresh final reprofile.

Cache full NCU replay by kernel-evidence fingerprint. Every correct candidate receives either a
fresh capture or an explicit unchanged-kernel reuse, plus parent- and baseline-relative deltas.
Put stable device images or build outputs in `kernel_profile.fingerprint_inputs`; absent those,
the controller conservatively fingerprints the local workload executable or declared source.
`init` owns baseline NCU, `evaluate` owns automatic candidate capture/reuse, and accepted
confirmation owns fresh final NCU. `--ncu ID` forces a replay for an exceptional attribution case.
Do not invoke NCU through a hand-written or patched harness plan. NCU replay time is profiling
cost, never application speed.

Do not emit inline programs, generated shell/awk/jq glue, per-candidate plans, progress readers,
JSON readers, or manual progress/report/finalization artifacts. Do not invoke `campaign.py`,
`batch_harness.py`, or `finalize.py` beside the controller. For a long tool session, wait on that
session at the longest practical interval; do not combine waits with `tail`, `ps`, or file reads.
Prefer non-login `/bin/sh`; establish required modules once in preflight.

Application/profiler output and samples stay under `raw/`. Use the controller's one-line decision
and named slice; read `experiment-brief.txt` only for recovery. Open one named failure log at most.
Accept only a correct, significant absolute gain with matching baseline identity and a reprofiled
target reduction whose collateral costs do not erase the result. Unknown measurements remain
`null`. Close with `tokens.py`; audit supplied rollouts with `session_audit.py` without loading
them.

For routine runs, call `route.py` and the controllers directly. Do not load `registry.md`,
`automation.md`, schema bodies, or framework source unless a compact controller error names one
of them as the unresolved dependency.
