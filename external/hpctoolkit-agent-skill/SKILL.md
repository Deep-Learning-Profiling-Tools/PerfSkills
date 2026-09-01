---
name: hpctoolkit-gpu-profile
description: Profile CUDA and GPU applications with HPCToolkit using hpcrun, hpcstruct, and hpcprof. Use when asked to profile, analyze, benchmark, or collect performance metrics for a CUDA executable or GPU kernel with HPCToolkit.
---

# HPCToolkit GPU Profiling

Use this skill when profiling an NVIDIA CUDA application or GPU kernel with HPCToolkit.

The goal is to:

1. verify that HPCToolkit and the target executable are usable,
2. determine supported profiling events,
3. collect measurements with `hpcrun`,
4. recover program structure with `hpcstruct`,
5. create an HPCToolkit database with `hpcprof`,
6. report the commands used, output directories, and any relevant errors.

Do not invent HPCToolkit events or options. When uncertain, inspect the installed HPCToolkit
command help and event list.

# 1. Verify HPCToolkit tools

Before profiling, check that the required tools are available:

```bash
command -v hpcrun
command -v hpcstruct
command -v hpcprof
```

If a required command is missing, report that clearly before attempting profiling.

When HPCToolkit was installed through Spack, inspect loaded packages if useful:

```bash
spack find -v hpctoolkit
```

If multiple HPCToolkit installations exist, determine which executable is currently active:

```bash
which hpcrun
which hpcstruct
which hpcprof
```

# 2. Consult HPCToolkit help when needed

Use the installed command-line help rather than guessing syntax.

For `hpcrun`:

```bash
hpcrun --help
```

To inspect available profiling events:

```bash
hpcrun -L
```

For `hpcstruct`:

```bash
hpcstruct --help
```

For `hpcprof`:

```bash
hpcprof --help
```

Use these commands whenever:

* an option is uncertain,
* an event may not be supported,
* HPCToolkit reports an invalid event,
* the installed HPCToolkit version may differ from expected behavior.

For GPU profiling, confirm that CUDA/GPU-related events are available before relying on them.

A useful search is:

```bash
hpcrun -L | grep -i -E 'gpu|cuda'
```

# 3. Verify the target CUDA executable

Confirm that the executable exists:

```bash
ls -lh ./<executable>
```

Run it normally before profiling:

```bash
./<executable>
```

Do not profile a program that already fails during normal execution unless the task is
specifically to investigate that failure.

For a CUDA application, optionally verify GPU visibility:

```bash
nvidia-smi
```

# 4. Preferred basic GPU profiling command

For a CUDA executable, begin with coarse-grained CUDA profiling plus a CPU/wall-clock timer
metric:

```bash
hpcrun \
  -e REALTIME@5000 \
  -e gpu=cuda \
  ./<executable>
```

Example:

```bash
hpcrun \
  -e REALTIME@5000 \
  -e gpu=cuda \
  ./matmul
```

Including a `REALTIME` event is useful because downstream analysis tools may expect a
conventional time metric in addition to GPU-specific metrics.

Do not assume that every machine supports every hardware event.

Inspect available events with:

```bash
hpcrun -L
```

before adding additional CPU hardware counters.

# 5. Optional CPU hardware metrics

If the task also asks for CPU-side behavior, inspect:

```bash
hpcrun -L
```

and select only events actually supported by the current system.

Potentially useful event categories include:

* CPU cycles
* instructions
* cache misses
* branch behavior
* frontend stalls
* backend stalls
* TLB misses

Example only, if supported by `hpcrun -L`:

```bash
hpcrun \
  -e REALTIME@5000 \
  -e CYCLES \
  -e INSTRUCTIONS \
  -e CACHE-MISSES \
  -e gpu=cuda \
  ./matmul
```

Never assume these exact event names are available on every machine.

# 6. Fine-grained GPU PC sampling

If the user specifically requests instruction-level GPU profiling or GPU stall analysis, first
inspect:

```bash
hpcrun -L
hpcrun --help
```

If GPU PC sampling is supported by the current HPCToolkit/CUDA environment, use:

```bash
hpcrun \
  -e REALTIME@5000 \
  -e gpu=cuda,pc \
  ./<executable>
```

Example:

```bash
hpcrun \
  -e REALTIME@5000 \
  -e gpu=cuda,pc \
  ./matmul
```

Do not automatically use PC sampling if the installed HPCToolkit or CUDA configuration does not
support it.

If PC sampling produces an unsupported-feature or compatibility error, fall back to:

```bash
hpcrun \
  -e REALTIME@5000 \
  -e gpu=cuda \
  ./<executable>
```

and clearly report that fine-grained PC sampling was unavailable.

# 7. Locate the measurement directory

After `hpcrun` completes, identify the generated measurement directory.

Commonly it has a name similar to:

```text
hpctoolkit-<program>-measurements
```

Inspect directories with:

```bash
ls -d hpctoolkit-*-measurements 2>/dev/null
```

Do not guess the measurement directory if multiple matching directories exist. Determine which
directory corresponds to the current profiling run.

For example:

```text
hpctoolkit-matmul-measurements
```

# 8. Run hpcstruct

For GPU profiling, run `hpcstruct` on the HPCToolkit measurement directory:

```bash
hpcstruct <measurement-directory>
```

Example:

```bash
hpcstruct hpctoolkit-matmul-measurements
```

For fine-grained GPU profiling where GPU control-flow reconstruction is useful, inspect:

```bash
hpcstruct --help
```

and, if supported and appropriate, use:

```bash
hpcstruct --gpucfg yes hpctoolkit-matmul-measurements
```

If `hpcstruct` fails, do not proceed as though structure recovery succeeded. Report its error
first.

# 9. Run hpcprof

After structure analysis succeeds, create the HPCToolkit performance database:

```bash
hpcprof <measurement-directory>
```

Example:

```bash
hpcprof hpctoolkit-matmul-measurements
```

After completion, locate the generated database:

```bash
ls -d hpctoolkit-*-database 2>/dev/null
```

A typical result is:

```text
hpctoolkit-matmul-database
```

# 10. Standard end-to-end workflow

For a CUDA executable named `matmul`, the normal workflow is:

```bash
./matmul

hpcrun \
  -e REALTIME@5000 \
  -e gpu=cuda \
  ./matmul

hpcstruct hpctoolkit-matmul-measurements

hpcprof hpctoolkit-matmul-measurements
```

The resulting flow is:

```text
matmul
  |
  | hpcrun
  v
hpctoolkit-matmul-measurements/
  |
  | hpcstruct
  v
structured measurements
  |
  | hpcprof
  v
hpctoolkit-matmul-database/
```

# 11. Troubleshooting policy

When a profiling command fails, inspect the error rather than repeatedly trying variants.

## HPCToolkit lacks CUDA support

If `hpcrun` reports that HPCToolkit was not compiled with NVIDIA CUDA support, stop GPU
profiling and report that a CUDA-enabled HPCToolkit build is required.

For a Spack installation, inspect:

```bash
spack find -v hpctoolkit
```

Look for whether CUDA support is enabled.

Do not claim GPU profiling succeeded when HPCToolkit itself lacks CUDA support.

## Unsupported event

If `hpcrun` reports an unsupported or unknown event:

```bash
hpcrun -L
```

Find a valid event from the installed implementation rather than guessing.

## CUDA application itself fails

Run:

```bash
./<executable>
```

without HPCToolkit.

If it fails normally, distinguish the CUDA/runtime/application failure from an HPCToolkit
profiling failure.

## hpcstruct option unavailable

Run:

```bash
hpcstruct --help
```

and adjust the invocation to the installed HPCToolkit version.

# 12. GPU metrics to look for

When reviewing an HPCToolkit GPU database, look for available metrics associated with:

* total GPU operation time,
* GPU kernel activity,
* explicit GPU copy time,
* host-to-device transfers,
* device-to-host transfers,
* GPU synchronization time,
* copy counts,
* synchronization counts,
* kernel launch behavior,
* occupancy-related information when available.

Do not assume every metric appears in every database. Use the metrics actually generated by the
current profiling run.

`read_profile.py` loads a database with [Hatchet](https://github.com/LLNL/hatchet) and prints
the GPU-bearing rows:

```bash
python3 read_profile.py [database-directory]
```

# 13. Reporting requirements

After profiling, report:

1. executable profiled,
2. exact `hpcrun` command,
3. profiling events used,
4. exact `hpcstruct` command,
5. exact `hpcprof` command,
6. HPCToolkit measurement directory,
7. HPCToolkit database directory,
8. any warnings or unsupported features,
9. whether the run used coarse CUDA profiling or fine-grained PC sampling.

Keep command output concise unless detailed diagnostics are requested.

# 14. Safety rules

Do not:

* delete existing HPCToolkit databases unless explicitly requested,
* overwrite previous profiling results without checking,
* invent unsupported hardware events,
* assume CUDA support merely because `hpcrun` exists,
* assume GPU PC sampling is supported,
* run an arbitrary executable without first understanding what it does,
* silently ignore errors from `hpcrun`, `hpcstruct`, or `hpcprof`.
