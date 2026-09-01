# Linux perf — mechanics and pitfalls

Loaded at stage 2. Tool-specific truth lives here so `SKILL.md` stays about method.

## Versions

| Version | Where | Status |
|---|---|---|
| 5.15.178 | this machine, `/usr/bin/perf`, `perf_event_paranoid = -1` (full access) | present; not yet exercised on a real workload — update this row after the first run |

`perf` is tied to the kernel. A `perf` newer or older than the running kernel usually works but
may warn or miss features; `perf --version` and `uname -r` should be close. Distros ship it as
`linux-tools-$(uname -r)`.

## Command recipes

```bash
# Cheap first pass: where does time go. Frame-pointer unwinding, ~1.01-1.05x.
perf record -F 999 --call-graph fp -o raw/perf.data -- <cmd>

# Same, when the binary lacks frame pointers (C++ with heavy inlining). Costlier, bigger files.
perf record -F 999 --call-graph dwarf,16384 -o raw/perf.data -- <cmd>

# Attach to an already-running job (MPI rank, long-lived server), 30s window
perf record -F 999 -g -p <pid> -- sleep 30

# System-wide (needs root); use when the cost may be outside your process
perf record -F 999 -a -g -- sleep 30

# Self time (exclusive) and inclusive time - separate columns, never blended
perf report -i raw/perf.data --stdio --no-children --percent-limit 0.5 --show-nr-samples
perf report -i raw/perf.data --stdio --children  --percent-limit 1.0

# Group by shared object first - tells you instantly if the cost is in a library
perf report -i raw/perf.data --stdio --sort=dso,symbol

# Counters. -r 5 repeats and reports variance. Keep events under the PMU slot count.
perf stat -e cycles,instructions,branch-misses,cache-references,cache-misses \
          -e L1-dcache-load-misses,LLC-loads,LLC-load-misses -r 5 -- <cmd>

# Intel top-down (Ice Lake and newer give all four levels)
perf stat --topdown -a -- <cmd>

# Line and instruction attribution for one hot symbol
perf annotate -i raw/perf.data --stdio -s <symbol>

# Precise attribution (PEBS/IBS) - use whenever blaming a specific instruction
perf record -e cycles:pp -F 999 -- <cmd>

# What this CPU actually supports
perf list; perf list hw cache; perf list | grep -i fp_arith
```

## Machine-readable output

`perf` has no stable JSON export across versions. In order of preference:

1. `perf stat -j -e ...` (JSON output, perf ≥ 5.x) — check it on your build first.
2. `perf data convert --to-json out.json` — availability varies by distro build; try it, do not
   depend on it.
3. `perf report --stdio -F overhead,sym,dso --percent-limit 0` and parse columns. Stable enough,
   but pin the `-F` field list so parsing does not silently shift.
4. `perf script` for per-sample records when you need custom aggregation.

Always keep the raw `perf.data` alongside the normalized JSON.

## Metric dictionary

| perf output | Schema field | Notes |
|---|---|---|
| `--no-children` overhead % | `hotspots[].pct_of_wall` | self/exclusive time share |
| `--children` overhead % | `hotspots[].total_time_s` (× wall) | inclusive of callees |
| `--show-nr-samples` count | `hotspots[].samples` | <100 samples ⇒ share is noisy; warn |
| `instructions` | `totals.instructions` | |
| `cycles` | `totals.cycles` | |
| instructions ÷ cycles | `totals.ipc` | compare against core issue width, not against 1.0 |
| `LLC-load-misses` × line size | `totals.bytes_dram` (approx) | approximation — prefetch and writebacks are not counted; mark it as approximate in evidence |
| `fp_arith_inst_retired.*` (x86) | `totals.flops` | weight by vector width: scalar×1, 128b×2/4, 256b×4/8, 512b×8/16 for double/single |
| `uncore_imc/data_reads/`, `data_writes` | `totals.bytes_dram` | the real bandwidth measurement; needs root |

Derived, with the exact denominator written down:

```
IPC                  = instructions / cycles
arithmetic intensity = flops / bytes_dram                       (bytes must be DRAM, not L1)
achieved BW (GB/s)   = bytes_dram / wall_time_s / 1e9
miss rate (level)    = <level>-misses / <level>-accesses        (name the level explicitly)
```

Compare achieved bandwidth against **measured** STREAM peak on the same node, not the vendor
headline — headline numbers make everything look memory-fine.

## Pitfalls

- **Skid.** Non-precise events attribute a sample to an instruction near, not at, the cost. Use
  `:pp` (Intel PEBS / AMD IBS) before blaming a specific line.
- **Missing frame pointers.** `--call-graph fp` silently yields truncated stacks on
  `-fomit-frame-pointer` builds (the default at `-O2` on many toolchains). Symptom: shallow or
  nonsense callers. Fix with `dwarf` unwinding or rebuild with `-fno-omit-frame-pointer`.
- **Inlining** collapses callee cost into the caller — the symbol you see may not be the code
  you would edit. Cross-check with `perf annotate` and the compiler's optimization report.
- **Multiplexing.** More events than PMU counters ⇒ time-sliced and scaled up. `perf stat`
  prints the fraction counted; below ~100% the numbers are extrapolations. Split into multiple
  runs instead of over-requesting.
- **`cache-misses` is not "LLC misses".** The generic aliases map to different PMU events per
  microarchitecture. Prefer explicit event names and record which ones you used.
- **Permissions.** `perf_event_paranoid`: 2 = user-space sampling only, 1 = +kernel, 0 = +raw
  tracepoints/most counters, −1 = everything. Containers additionally need `CAP_PERFMON` (or
  `--privileged`). `kptr_restrict=1` hides kernel symbols, showing `[unknown]` kernel frames.
- **VMs and cloud instances** usually expose no PMU at all — `perf list hw` comes back empty.
  Sampling on `cpu-clock` still works; all counter-based conclusions do not.
- **`perf stat` counts descendants** by default. If the run command is a wrapper script,
  `mpirun`, or a launcher, you are measuring the whole tree.
- **Frequency scaling and turbo** move cycle counts between runs. Pin the governor to
  `performance` for counter work, or report the variance.
- **Hyperthreading/SMT** siblings contend; a 2× "speedup" from disabling SMT is a machine
  configuration change, not a code result.
- **Sampling frequency vs. periodic workloads.** `-F 999` (not 1000) avoids locking onto
  timer-periodic behavior.
- **File size.** `--call-graph dwarf` on a long run produces gigabytes. Bound the run or use
  frame pointers.

## Smoke test

When results look implausible, separate "the code is weird" from "perf is misconfigured":

```bash
cat > /tmp/pw_smoke.c <<'EOF'
#include <stdlib.h>
volatile double sink;
void hot(void){ for(long i=0;i<800000000L;i++) sink += 1.0; }   /* ~all the time */
void cold(void){ for(long i=0;i<8000000L;i++)  sink += 1.0; }   /* ~1%           */
int main(void){ hot(); cold(); return 0; }
EOF
gcc -O2 -g -fno-omit-frame-pointer /tmp/pw_smoke.c -o /tmp/pw_smoke
perf record -F 999 --call-graph fp -o /tmp/pw_smoke.data -- /tmp/pw_smoke
perf report -i /tmp/pw_smoke.data --stdio --no-children --percent-limit 0.5
```

Expect `hot` at ~99% with resolved symbols. If symbols are `[unknown]`, or the split is wrong,
the problem is the setup — fix it before interpreting a real profile.
