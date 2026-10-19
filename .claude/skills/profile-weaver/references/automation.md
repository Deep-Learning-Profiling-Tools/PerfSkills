# Automated execution protocol

Read this reference when creating or debugging a harness plan. Routine runs should execute the
plan and consume `agent-brief.txt` without loading this document.

## Plan shape

Paths are relative to the repository root unless absolute. `{repo}`, `{run_id}`, and `{run_dir}`
are expanded in strings. `argv` is an argument array and is never passed through a shell.

```json
{
  "version": 1,
  "run_id": "example-run",
  "run_dir": "runs/example-run",
  "env": {"CUDA_VISIBLE_DEVICES": "0"},
  "fingerprint_env": ["CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS"],
  "stages": [
    {
      "id": "profile",
      "argv": ["python3", "tool-wrapper.py", "--output", "{run_dir}/profile.baseline.json"],
      "cwd": "{repo}",
      "requires": [],
      "fingerprint_inputs": ["tool-wrapper.py", "build/app", "runs/example-run/request.json"],
      "produces": ["{run_dir}/profile.baseline.json"],
      "timeout_s": 600,
      "cache": true
    }
  ]
}
```

Every stage saves full output as `raw/harness/<stage>.stdout.txt` and `.stderr.txt`. A cached
stage is reused only if its command, environment, declared inputs, and produced-file hashes
match. Declare every file that can change the result in `fingerprint_inputs`.
List inherited environment variables that can change behavior in `fingerprint_env`; otherwise
they are preserved for execution but cannot invalidate a cached stage.

Use explicit `bash -lc` as the first two `argv` entries only when an existing build or launcher
truly requires shell syntax. Keep untrusted strings out of shell commands.

## Recommended stages

1. `preflight`: versions, availability, permissions, machine, build/run contract.
2. `baseline-benchmark`: correct unprofiled samples.
3. `capture-baseline`: immutable raw profiler capture.
4. `normalize-baseline`: normalized profile and compact tool details.
5. `locate-source`: bounded ranges for measured hotspots.
6. `build-variant`: one declared change.
7. `benchmark-variant`: paired adaptive A/B plus oracle.
8. `reprofile-winner`: same profiler scope and settings.
9. `report`: trust gates and final disposition.

Do not combine capture and normalization in a cacheable stage. A parser improvement should
rerun normalization without repeating an expensive workload.

## Cached preflight

Before the main plan, put all tool, compiler, device, profiler-capability, build, launch, and
oracle probes in `preflight-plan.json`. Run `shared/scripts/preflight.py` once. It fingerprints
the declared inputs, selected environment, and resolved tool paths; captures full streams under
`raw/preflight/`; extracts only declared version/device/capability facts; classifies common
compiler, shared-library, GPU-target, driver, permission, and memory failures; and emits one line
plus `preflight-brief.txt`. A matching contract and produced-file state returns a cache hit.

Do not repeatedly call `command -v`, dump full profiler help, search the filesystem for compiler
or support files, or retry a launch from the agent. Declare those checks once. On failure, consume
the failure class and open only that check's raw log. Use an explicit `bash -lc` argv only when an
existing environment script requires a shell. Invoke framework commands through a non-login
`/bin/sh` when the agent tool supports it so unrelated site startup/module diagnostics do not
enter context.

## Context controls

- Default `agent-brief.txt` budget: 320 exact `o200k_base` tokens when available.
- `agent-brief.json`, the expanded `agent-view.json`, profiles, hashes, samples, and full commands
  stay in the evidence plane.
- The brief labels measured facts, source facts, and hypotheses separately. Do not collapse them.
- Source slices default to two targets. Expand one named dependency with `--symbol` or one caller
  window with `--call-site`; never browse the application to compensate for missing attribution.
- A drill-down must be named and capable of changing the next decision.

## Experiment controller

The experiment controller keeps workspace copies, plans, progress tracking, and report assembly
inside reusable scripts. Routine optimization runs should not reproduce that orchestration in
tool arguments.

For a new run, create one `experiment-plan.json`. It declares the baseline source, source
fingerprint globs, one inherited build/run contract, metric extractor, oracle, sampling bounds,
and the already-routed child's per-iteration stages. The controller is tool-neutral; routing still
selects the cheapest child capable of answering the question. Validate and initialize it once:

```bash
python3 shared/scripts/validate.py shared/schema/experiment-plan.schema.json \
  runs/<run-id>/experiment-plan.json
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json init
```

The required `iteration_profile` block names the immutable baseline profile, candidate normalized
profile, candidate source map, and argument-array stages. `{workspace}`, `{id}`, `{profile_dir}`,
`{profile_raw}`, `{profile_output}`, and `{baseline_profile}` join the normal plan variables.
Capture files and stage streams belong under `{profile_raw}`; small normalized evidence belongs
under `{profile_dir}`.

```json
{
  "iteration_profile": {
    "baseline_profile": "{run_dir}/profile.baseline.json",
    "profile_output": "{profile_dir}/profile.json",
    "source_targets": "{profile_dir}/source-targets.json",
    "max_delta_items": 2,
    "stages": [
      {
        "id": "capture-and-normalize",
        "argv": ["python3", "child-wrapper.py", "--workspace", "{workspace}",
                 "--raw", "{profile_raw}", "--output", "{profile_output}"],
        "fingerprint_inputs": ["child-wrapper.py"],
        "produces": ["{profile_output}"]
      },
      {
        "id": "locate",
        "argv": ["python3", "shared/scripts/locate_source.py", "--profile",
                 "{profile_output}", "--run-dir", "{profile_dir}", "--source-root",
                 "{workspace}", "--output", "{profile_dir}/source-targets.json"],
        "produces": ["{profile_dir}/source-targets.json"]
      }
    ]
  }
}
```

The example uses one wrapper for brevity. Keep expensive capture and cheap normalization as
separate stages when the child supports replaying its raw artifact. Include child scripts,
configuration, and other result-changing files in `fingerprint_inputs`.

For one-kernel NVIDIA counter evidence, declare the policy once. The controller derives the
application command, working directory, and baseline/variant kernel regex from the inherited
execution and attribution contracts:

```json
{
  "kernel_profile": {
    "enabled": true,
    "section": "all",
    "launch_count": 1,
    "timeout_s": 900,
    "tool_version": "<cached-preflight-version>",
    "fingerprint_inputs": ["{workspace}/<device-image-or-built-executable>"]
  }
}
```

`init` captures and caches baseline NCU. Every correct candidate automatically receives an NCU
disposition. A changed fingerprint runs a bounded full-set replay; an identical fingerprint
reuses the parent's evidence with zero replay time and says why. The capsule includes current
profiling time, registers, occupancy, scheduler eligibility, leading stall, load-sector
utilization, and bounded parent- and baseline-relative deltas. If explicit inputs are absent, the
controller uses a workload executable inside the candidate workspace or conservatively falls back
to the declared source fingerprint. `--ncu <id>` forces a fresh replay; it is not required for
normal evaluation. Accepted confirmation performs a fresh final NCU replay. Do not create or patch
`harness-plan.json` for these stages, invoke the NCU wrapper manually, or generate candidate NCU
briefs.

`experiment.py prepare-batch` copies up to three baseline or measured-parent workspaces in one
exchange. The agent edits source there, then screens the batch in one call:

```bash
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json prepare-batch \
  --candidate ordering:baseline:data-layout \
  --candidate precompute:baseline:invariant-precompute
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json evaluate \
  ordering precompute
```

The controller creates patches and internal campaign plans, runs candidates sequentially, and
profiles every correct candidate with the routed child. It validates the normalized profile,
builds an impact-ranked diff against the immutable baseline, locates bounded source ranges, caches
the evidence by source/tool fingerprint, updates the hotspot frontier, and emits one JSON line.
The line contains only each candidate's profile wall/GPU movement, declared target movement,
largest gain/regression, remaining hotspot and named slice, NCU capture/reuse status and compact
inefficiency deltas, plus artifact paths. Reused NCU evidence is represented as `unchanged`
instead of repeating the parent's metric vector. Tool and application streams stay under
`raw/iteration/<candidate>/`. Use individual `prepare` only for an inherited follow-up. Screening
stops after adaptive candidate pairs; it does not also run finalist confirmation. Follow-up
candidates may inherit from a measured parent using `--parent <id>` without copying its plan into
context.

Confirm only the strongest one or two after structural search:

```bash
python3 shared/scripts/experiment.py runs/<run-id>/experiment-plan.json confirm ordering fusion
```

Confirmation consumes stored screening summaries and runs only the interleaved baseline/finalist
stage. It returns the cached per-candidate profile delta instead of collecting another trace. The
configured 15 pairs are a ceiling, not a fixed count: sampling stops after at least five as soon
as every trust gate resolves. Fresh reprofile and finalizer stages execute once for the accepted
winner. Use the `status` subcommand and `experiment-brief.txt`; do not write progress or
JSON-reading helpers.

Routine work must not emit `python - <<`, `python -c`, generated shell/awk/jq programs, or
run-local orchestration. Do not directly combine `batch_harness.py`, `campaign.py`, and
`finalize.py` with the experiment controller. They remain available for direct harness and
campaign workflows. Application
stdout/stderr and full samples remain under `raw/`; open one named stream only for a failure.

## Self-evolution limits

Within a run, the ledger evolves the search frontier from every candidate delta: it records target
movement, collateral regression, and the next measured hotspot/source slice. Across runs, it
learns observed profiler cost, run count, gate failures, and outcomes. Three runs are required
before a prior is eligible. Priors can order equally capable tools or suggest a run cap. The
current run still measures availability, correctness, significance, and the target mechanism.
Policy data never edits a `SKILL.md`, source file, schema, or correctness threshold.
