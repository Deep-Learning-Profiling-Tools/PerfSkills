# Native HPCToolkit and GPA evidence

The adaptive router has built-in collection adapters for HPCToolkit and GPA. These collect
evidence from the same already built executable and declared command; they do not run the
standalone vendored optimization loop, edit application source, rebuild, change arguments, or
require entries in a benchmark corpus. MPI/scheduler arguments remain outside the `hpcrun`
prefix. A launcher whose application token cannot be resolved is blocked explicitly.

Use missing capabilities first:

| Question | Provider / evidence | Capability check |
|---|---|---|
| Which host call path launches an expensive GPU operation? | HPCToolkit GPU operation seconds, call paths, available transfer/sync metrics | CUDA event advertised; compatible V4 reducer available |
| Which GPU instruction/source line carries sampled stalls? | HPCToolkit PC metrics when recorded | A positively advertised PC event; negative compatibility notes override text matches |
| Which instruction dependence is blamed for a stall? | GPA literal `GINS:LAT_*`, source line, blamed ratio and advisor finding | Compatible PC sampling plus GPA-enabled `hpcprof` and explicitly configured GPU architecture model |
| How much cache/sector traffic or achieved occupancy? | NCU hardware counters | Verified counter sections; context/PC data do not substitute |
| How much host synchronization or timeline overlap? | NSYS timeline, or another provider only when those metrics are actually available | Full requested capability coverage |

Commands during an adaptive journey:

```
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json evaluate a --need call-context
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json evaluate b --need causal-stall-attribution
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json evaluate c --provider pw-hpctoolkit --need gpu-hotspot
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json inspect --section evidence --candidate c --group contexts
python3 shared/scripts/experiment.py runs/<id>/experiment-plan.json inspect --section costs
```

GPA is considered automatically for a source/causal question at a plateau, with an explicit
capability fallback if unavailable. It is not a mandatory pass. A successful recent mechanism
capture has a short cooldown; a plateau alone does not justify repeating it immediately.
Explicit requests remain available. Equivalent providers with measured costs are compared in
seconds; ordinal priors are used when no equivalent provider has a measurement. An uncalibrated
new provider does not erase a known cheaper choice. Distinctive questions or explicit provider
requests can calibrate it. PC and coarse-context cost histories use separate keys.

## Configuration and probes

Provider tools resolve from declared paths, installation roots, or `PATH`. Supported roots:

- `PW_HPCTOOLKIT_ROOT` / `HPCTOOLKIT_ROOT`: stock HPCToolkit installation.
- `PW_GPA_TOOLCHAIN`: parent of `hpctoolkit/bin` and `bin` in a GPA installation.
- `PW_GPA_ROOT`: GPA project root, with the toolchain under `gpa/`.

A workload may declare the following under `profiling`; machine-local configuration may put
the same provider map in ignored `shared/config/profilers.local.json`:

```json
{
  "providers": {
    "pw-hpctoolkit": {"root": "/installation/hpctoolkit", "jobs": 4},
    "pw-gpa": {"root": "/installation/gpa", "gpu_arch": "<verified-supported-model>"}
  }
}
```

The local file is the map itself (`{"pw-hpctoolkit": {...}}`), without the `providers` wrapper.
Individual `hpcrun`, `hpcstruct`, and `hpcprof` paths can be declared in the provider config or
as provider-qualified `profiling.tool_paths` entries such as `pw-gpa.hpcrun`. Build/runtime library
requirements can be supplied as provider `env`. Configuration does not make a tool available:
cached event/help probes must succeed. Tool changes and library environment changes invalidate
the probe cache. Probe stdout, including very large CPU event catalogs, stays on disk.

There is no default architecture-model substitution. A supported toolchain/model must be supplied
for GPA. Coarse CUDA activity support does not imply PC sampling. In the tested Spack installation,
`hpcrun -L` explicitly reports that `gpu=cuda,pc` is unsupported on CUDA 13; that path is disabled.

The GPA model must also be verified from installed help listing supported architectures, or a
configured `source_root` containing the model classes **and** their analyzer initialization.
The requested model must match the visible GPU(s). Upstream GPA at
`4d1cef277473397f256c5b73dd2a2bdef2d4201e` uses HPCToolkit
`f98d3e7ec25efdf12a3b5e91406429f18819a1c7`, models only A100/V100, and silently initializes
V100 for other names. It cannot produce validated B200 advice simply by setting `gpu_arch=B200`.
Its legacy `cuptiActivityConfigurePCSampling` path was removed in CUDA 13. Porting collection
and validating a Blackwell architecture model are upstream engineering work, not PATH fixes.

## Current CUDA PC sampling

`evaluate ID --need gpu-pc --need instruction-stalls` can select CUPTI's current PC API through
the `pw-cupti` child, independently of stock HPCToolkit's legacy PC support. The device API is
probed before routing. Collection and decoding use cached builds of the installed SDK samples;
the installed files remain unchanged. A cached source copy changes output naming only, adding
PID/context suffixes after the absolute raw path. The application command/cwd/build remain fixed.

This produces literal sampled-warp stall metrics and instruction offsets. Collection covers the
process; reduction targets the timing-ranked kernel. Missing source correlation remains null.
It does not provide GPA causal/dataflow blame, kernel durations, cache traffic, or achieved
occupancy. `*_not_issued` is kept separately from its parent population. Nonzero exit, failed
oracle, zero samples, drops, or changed binary identity prevent complete evidence. Raw logs and
all PC records are retained; bounded stalls/locations are delivered inline and via
`inspect --section evidence --candidate ID --group locations`.

Activity and PC histories are separate. Coarse activity timing cannot calibrate a PC capture,
and vice versa. General candidate acquisition priority is
`f * (1 - 1 / roofline_ceiling) * uncertainty / profiler_wall_seconds`; without Roofline,
`f` gives only an Amdahl removal bound. Unknown costs remain unknown. This is an acquisition
heuristic, not expected speedup or proof of an attainable optimization. Historical small-share
or near-saturated targets receive a fresh ROI map first, and changed dominant symbols invalidate
old bounds. A refresh cannot permanently suppress deeper questions. Explicit requests bypass
this scheduling. Startup and final NSYS/NCU evidence remain unchanged.

## Evidence and limits

Every attempt keeps isolated measurements/databases and exact stage commands and elapsed wall
times and executable identity. The capture oracle must pass. When installed help advertises
structure caching, adapters enable HPCToolkit's content-based cache, separated by provider and
coarse/PC mode; a pre-existing user cache is respected. HPCToolkit handles invalidation and
relocation of identical binaries. Reduction uses all CCT entry points, retains full C++
signatures, and avoids summing inclusive ancestors repeatedly. Measured kernel seconds are not
called GPU busy union. Missing source lines, metrics, timers, or requested capabilities remain
unknown/partial. The V4 reader currently requires `hpcanalysis`; unsupported formats fail clearly.

GPA's definition line is distinct from its blamed instruction line: the latter is taken from
the address record following the literal stall token. Advisor predicted speedups are marked as
predictions and never enter candidate ranking. Known measured kernel targets bound GPA reduction;
an absent target is not replaced with an inactive kernel. Full raw advice remains available.

Adaptive evaluations deliver a bounded evidence packet inline, including provider provenance,
freshness, observed kernel/calling context or literal GPA finding, source references and explicit
unknowns. Named evidence groups provide detail. Compiler resources are matched to the observed
kernel instead of the first compiled variant. Historical compiled names are labeled if execution
has not been re-established. Candidate inspection never substitutes baseline evidence.

NCU-based symptom classifications are labeled `DERIVED FROM NCU`, not `GPA`. Actual GPA evidence
has its own provider identity, literal tokens and source records. Final winner NSYS/NCU evidence
and source slices are exposed separately from screening snapshots.

Runtime cost events record initialization, campaign commands, routing/static evidence, captures
(including failures), and final evidence. Cached retries contribute only their actual command
wall time; old benchmark samples are not counted again. Benchmark results split build, warmup,
screening, correctness-oracle and confirmation time. Recorded controller totals exclude agent
thinking/token-generation time and do not reconstruct missing historical events.

Primary references: [HPCToolkit GPU monitoring](https://hpctoolkit.gitlab.io/hpctoolkit/users/gpu/gpu.html),
[GPA implementation](https://github.com/Jokeren/GPA). Installed help/events and actual collected
metrics are authoritative for each invocation.
