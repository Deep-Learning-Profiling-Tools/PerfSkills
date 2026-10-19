# NVIDIA Nsight Systems — mechanics and pitfalls

Load this at stage 2. The installed command help is the source of truth; NVIDIA changes trace
providers, report scripts, export schemas, and permissions between releases.

## Version record

| Version | Where | Status |
|---|---|---|
| 2025.5.1.121 | this machine, `nsys` on `PATH` | Core `cuda,nvtx,osrt` capture tested; CUDA API, kernel, memory-time, memory-size, and launch/execute CSV reports verified on a short XSBench run. This verifies command mechanics only; workload overhead remains unmeasured |

Update this table after the first real investigation. Do not turn the smoke test into a claim
about representative overhead or application performance.

## Capability discovery

```bash
nsys --version
nsys status --environment
nsys profile --help=trace
nsys profile --help=cuda
nsys profile --help=sample
nsys profile --help=nvtx
nsys profile --help=mpi
nsys profile --help=output
nsys stats --help-reports
nsys stats --help-formats
nvidia-smi
```

Some releases expose provider names only in `--help=trace`; the draft command
`nsys profile --trace-help` is not portable and is rejected by the installed 2025.5 build.
Never copy provider names or report names from this file without checking local help.

Useful feature discovery, only when the question needs it:

```bash
nsys profile --gpu-metrics-devices=help
nsys profile --gpu-metrics-set=help
nsys profile --cpu-core-events=help   # may fail until privileges permit counters
nsys profile --os-events=help
```

## Trace-provider catalog

Select the smallest bundle that answers the question. The following families are known to
exist in the installed 2025.5 build, but availability and useful output still depend on the
target process and linked libraries.

| Provider family | What it can place on the timeline | Cost and interpretation notes |
|---|---|---|
| `cuda` | CUDA Runtime/Driver APIs, kernels, memory operations, synchronization | Core provider. Shows when operations happen, not why a resident kernel is slow. |
| `cuda-hw` | CUDA hardware scheduling detail advertised by supported versions/platforms | Treat as an optional, platform-specific second pass; confirm semantics and overhead locally. |
| `nvtx` | User/framework phases, categories, and named ranges | Best ROI and attribution aid; names come from the application/framework and are not profiler conclusions. |
| `cublas`, `cudnn`, `cusolver`, `cusparse` | CUDA-library API calls | Add the library actually used. Spelling is version-sensitive (`cuDNN` aliases may also appear). |
| `cublas-verbose`, `cuDNN-verbose`, `cusolver-verbose`, `cusparse-verbose` | More library call/argument detail | Can enlarge traces and expose argument metadata; use only for a concrete library question. |
| `openmp`, `openacc` | Host runtime/offload events | Correlate directive runtime work with CUDA activity. Compiler diagnostics are still needed to prove what was generated. |
| `mpi`, `oshmem`, `ucx` | Communication/runtime calls and selected message context | Useful beside GPU gaps. It does not replace a scale study or dedicated wait-state analysis. |
| `osrt` | Long OS runtime calls such as waits, locks, and selected I/O-related APIs | Threshold-sensitive; lowering the threshold can create large traces and high overhead. |
| `python-gil` | Python GIL ownership/serialization | Add for Python-driven GPU workloads when GIL activity may explain launch gaps. |
| `gds` | GPU Direct Storage events when the build labels the provider supported/experimental | Use only after local help confirms it and the workload actually uses GDS. |
| `opengl`, `opengl-annotations`, `vulkan`, `vulkan-annotations`, `nvvideo` | Graphics/video API and GPU workload context | Outside most HPC runs but valid for mixed visualization or media pipelines. |

The installed provider list does not advertise a standalone `nccl` source. NCCL work may be
visible as CUDA kernels and NVTX ranges when the library emits them, but that is not an NCCL
API trace. Enable a future `nccl` provider only if that installed version lists it.

## Orthogonal event and metric switches

These extend a provider trace. They are not all compatible, low-cost, or permitted.

| Feature | Discovery / typical switch | Use |
|---|---|---|
| CUDA event dependencies | `--cuda-event-trace=auto|true` | Device-side event completion and stream dependency correlation; may perturb event behavior and overhead. |
| CUDA Graph detail | `--cuda-graph-trace=graph|node` plus version-supported launch origin | `graph` is lower volume; `node` can be expensive. |
| Unified Memory faults | `--cuda-um-cpu-page-faults=true`, `--cuda-um-gpu-page-faults=true` | Migration/fault evidence; both support and overhead are platform-specific. |
| GPU memory footprint | `--cuda-memory-usage=true` | Timeline of allocations/usage; can add substantial overhead. |
| CUDA API backtraces | `--cudabacktrace=<class>:<threshold>` with CPU sampling | Links slow launch/memory/sync calls to host stacks; threshold and sampling cost matter. |
| GPU metrics | `--gpu-metrics-devices=...`, optional advertised set/frequency | Sampled device-level utilization/clock/activity. These are not per-kernel replay counters. |
| GPU context switches | `--gpuctxsw=true` | Detect context sharing/preemption where supported. |
| CPU IP samples | `--sample=process-tree` with `--backtrace=lbr|fp|dwarf|none` | Attribute host work in GPU gaps. Backtrace choice must match architecture/build. |
| CPU context switches | `--cpuctxsw=process-tree` | Shows scheduled/runnable gaps in the process tree. System-wide mode needs stronger privileges. |
| CPU/OS event samples | `--event-sample`, `--cpu-core-events`, `--os-events` | System-scoped counter samples; typically needs root or paranoid level ≤0. |
| OS runtime detail | `--osrt-threshold`, `--osrt-backtrace-threshold`, optional file access | Lower thresholds improve coverage but can explode overhead and trace size. |
| Linux ftrace | `--ftrace=...` | Kernel event context; requires root and a specific hypothesis. |
| NIC/IB metrics | `--nic-metrics=true` and advertised IB switches | Network activity context on supported hardware; does not by itself prove MPI imbalance. |
| Fork-before-exec | `--trace-fork-before-exec=true` | Only when missing pre-exec child activity matters; help warns of crash/deadlock risk. |

Record every non-default switch. A trace with extra metrics is a different measurement
configuration and cannot be compared silently with a minimal baseline.

## Command recipes

### Core CUDA timeline

```bash
nsys profile \
  --trace=cuda,nvtx,osrt \
  --sample=none --cpuctxsw=none \
  --stats=false --force-overwrite=true \
  --output=runs/<run-id>/raw/baseline \
  -- <run cmd>
```

### Directive-runtime context

After confirming the providers:

```bash
nsys profile \
  --trace=cuda,nvtx,osrt,openmp,openacc \
  --sample=none --cpuctxsw=none \
  --stats=false --force-overwrite=true \
  --output=runs/<run-id>/raw/directives \
  -- <run cmd>
```

Use the provider matching the program; an application normally does not need both OpenMP and
OpenACC. Preserve its existing compiler and directives.

### CUDA-library context

```bash
nsys profile \
  --trace=cuda,nvtx,osrt,cublas,cudnn,cusolver,cusparse \
  --sample=none --cpuctxsw=none \
  --stats=false --force-overwrite=true \
  --output=runs/<run-id>/raw/libraries \
  -- <run cmd>
```

Remove unused providers. Add one verbose provider only when its argument detail can decide the
next action.

### CPU attribution inside gaps

```bash
nsys profile \
  --trace=cuda,nvtx,osrt \
  --sample=process-tree --cpuctxsw=process-tree --backtrace=fp \
  --stats=false --force-overwrite=true \
  --output=runs/<run-id>/raw/cpu-gap \
  -- <run cmd>
```

Use `fp` only for a frame-pointer build. Use a locally supported LBR or DWARF mode otherwise,
measure its overhead, and bound the capture.

### MPI/UCX context

Identify the MPI implementation before setting it. Preserve the launcher, rank count, affinity,
and GPU mapping. One safe multi-process naming pattern uses documented hostname/PID expansion:

```bash
mpirun -np <N> nsys profile \
  --trace=cuda,nvtx,osrt,mpi \
  --mpi-impl=<openmpi|mpich> \
  --sample=none --cpuctxsw=none \
  --stats=false --force-overwrite=true \
  --output='runs/<run-id>/raw/rank.%h.%p' \
  -- ./app <args>
```

Adapt this to the scheduler/launcher already used. `%q{ENV_VAR}` is also available on versions
whose `--help=output` documents it. Never assume a vendor-specific rank environment variable;
inspect the launcher and verify that every rank produced a distinct report. Add `ucx` only when
the application uses UCX and its events can answer the framed question.

### NVTX-bounded capture

```bash
nsys profile \
  --trace=cuda,nvtx,osrt \
  --capture-range=nvtx \
  --nvtx-capture='steady_state@my_domain' \
  --capture-range-end=stop \
  --sample=none --cpuctxsw=none \
  --stats=false --force-overwrite=true \
  --output=runs/<run-id>/raw/roi \
  -- <run cmd>
```

Confirm all three capture options locally. The range must execute, names/domains must match
exactly, and baseline and variant must trigger the same semantic work.

### Summaries and event exports

```bash
nsys stats \
  --force-export=true \
  --report cuda_api_sum,cuda_gpu_kern_sum,cuda_gpu_mem_time_sum,cuda_gpu_mem_size_sum,cuda_kern_exec_sum \
  --format csv --output runs/<run-id>/raw/baseline \
  --force-overwrite=true runs/<run-id>/raw/baseline.nsys-rep

nsys stats \
  --force-export=true \
  --report cuda_gpu_trace,cuda_kern_exec_trace,cuda_api_trace \
  --format csv --output runs/<run-id>/raw/baseline-events \
  --force-overwrite=true runs/<run-id>/raw/baseline.nsys-rep

nsys export --type=sqlite --force-overwrite=true \
  --output=runs/<run-id>/raw/baseline.sqlite \
  runs/<run-id>/raw/baseline.nsys-rep
```

`--force-export=true` deliberately rebuilds the analysis SQLite database from the immutable
`.nsys-rep`; this avoids stale-export rejection after a report changes. Reuse an existing
database only when its provenance and timestamp have been checked explicitly.

Additional confirmed summaries can include `cuda_api_gpu_sum`, `cuda_gpu_sum`, `nvtx_sum`,
`nvtx_gpu_proj_sum`, `osrt_sum`, `openmp_sum`, `openacc_sum`, `mpi_event_sum`,
`mpi_msg_size_sum`, `um_sum`, and `um_total_sum`. Trace variants such as `mpi_event_trace` and
`nvtx_gpu_proj_trace` are for bounded detailed analysis.

## Metric dictionary

| Nsight Systems output | ProfileWeaver use | Constraint |
|---|---|---|
| `cuda_gpu_kern_sum` | Kernel `self_time_s`, `calls`, duration distribution | Its percentage denominator is GPU-kernel time, not application wall time. |
| `cuda_gpu_mem_time_sum` | Memory-operation time by type | Summed time may overlap kernels or other copies. |
| `cuda_gpu_mem_size_sum` | Transfer bytes/counts by type | Bytes alone do not establish critical-path cost. |
| `cuda_api_sum` | Host API/synchronization time and calls | Host time; never add directly to device time. |
| `cuda_kern_exec_sum` | Launch/API, queue, and execution timing relationship | Column names/definitions are versioned; cite the local report documentation. |
| `cuda_gpu_trace` | Start, duration, device, context, stream, operation | Input for interval union, gap, and overlap calculations. |
| `cuda_kern_exec_trace` | Per-launch host-to-device timing | Input for launch-delay and tiny-kernel analysis. |
| `nvtx_sum` / `nvtx_gpu_proj_sum` | Phase/ROI duration and GPU work associated with named ranges | Projection follows launch association and may not equal strict wall-clock overlap. |
| `openmp_sum` / `openacc_sum` | Directive-runtime event time/count | Runtime context, not compiler proof of safe/correct offload. |
| `mpi_event_*` / `mpi_msg_size_sum` | MPI call/event timing, peer/rank/message context when present | Does not alone separate inherent transfer from imbalance or establish scaling. |
| `um_*` reports | Unified Memory migration/page-fault evidence | Only valid when those events were enabled and captured. |
| GPU metrics timeline | Sampled device-level activity/clock/utilization | Sampling, not per-kernel counters; do not infer occupancy or roofline. |

All durations from CSV reports are commonly nanoseconds, but verify the emitted unit header
before converting. Preserve raw units in evidence notes.

## Interval calculations

For a selected device and ROI:

1. Clip every kernel and memory-operation interval to the ROI.
2. Sort by start time.
3. Merge intervals that overlap or touch.
4. Sum merged lengths for `gpu_busy_s`.
5. `gpu_idle_s = roi_duration_s - gpu_busy_s`.
6. Intersect copy intervals with kernel intervals to measure copy/compute overlap; do not infer
   overlap from equal totals.

For multiple GPUs, compute each device separately. A union across devices answers “was any GPU
busy,” while a sum answers “how much aggregate device work occurred”; they are different
questions. The current profile schema has one `gpu_busy_s`, so name one device/definition or
leave it null and carry the per-device results in bottleneck evidence.

## Interpretation pitfalls

- **Summary percentages use different denominators.** Kernel %, API %, and memory-operation %
  are not wall-time shares. Recompute against the recorded ROI.
- **Asynchrony breaks naive addition.** A CUDA launch API, its kernel, a copy, and CPU work may
  overlap. Interval unions and intersections are required for wall-time claims.
- **A synchronization call is where waiting surfaced, not automatically its root cause.** Trace
  backward to the dependency, producer, transfer, or host delay before choosing a rewrite.
- **A copy can be large and harmless.** Only non-overlapped critical-path time licenses a
  transfer bottleneck claim.
- **Many kernels are not automatically launch-bound.** Show short kernel durations plus material
  launch/API/queue delay or CPU gaps.
- **GPU metrics are sampled system signals.** They do not replace Nsight Compute replay metrics
  for occupancy, cache, memory throughput, or scheduler stalls.
- **First-use costs distort traces.** CUDA context creation, module loading, JIT, allocator
  growth, and autotuning may belong to startup. Keep them only when startup is in scope;
  otherwise use a repeatable warmup and ROI.
- **Trace volume causes dropped events and observer effect.** Provider count, verbose libraries,
  low OSRT thresholds, backtraces, CPU sampling, UM faults, and metrics all add cost. Split
  hypothesis-specific passes and record dropped-event warnings.
- **Kernel names are not source locations.** Templates and generated offload kernels may be
  mangled or fused. Use NVTX, build artifacts, or a source-attributing child before editing an
  uncertain site.
- **No CUDA activity has several explanations.** No offload, wrong process tree, delayed child,
  unsupported provider, premature capture end, or failure before initialization are all
  possible. Inspect logs and target output instead of selecting the convenient explanation.
- **MPI traces need unique files and aligned clocks.** Preserve rank mapping and verify report
  count. Cross-node timestamp conclusions require sufficiently synchronized clocks.
- **Multi-tenant GPUs confound utilization and gaps.** Context-switch traces or device/process
  evidence may reveal other clients; otherwise mark exclusivity unknown.
- **GUI views are evidence only when exported or described reproducibly.** Keep the `.nsys-rep`
  and record the view, filters, ROI, device, process, and measured interval used for a claim.

## Smoke test

Use a known-correct, short GPU command from the target project rather than inventing a new
microbenchmark:

```bash
nsys profile --trace=cuda,nvtx,osrt --sample=none --cpuctxsw=none \
  --stats=false --force-overwrite=true --output=/tmp/pw-nsys-smoke -- <tiny GPU run>
nsys stats --report cuda_api_sum,cuda_gpu_kern_sum,cuda_gpu_mem_time_sum \
  --force-export=true --format csv --output /tmp/pw-nsys-smoke --force-overwrite=true \
  /tmp/pw-nsys-smoke.nsys-rep
```

Expect a report plus CUDA API and kernel rows. An empty CUDA report is a setup/offload finding,
not permission to fabricate the missing timeline.
