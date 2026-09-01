# DrGPUM sees PyTorch allocator segments, not tensors

**Finding:** without `torch-monitor`, every object DrGPUM reports for a PyTorch
process is a *caching-allocator segment*, not a tensor. The top-ranked findings
on `train.py` — a 20 MB object that is never accessed and never freed — are
artifacts of that, not inefficiencies in the application.

Captured on an RTX 3090, CUDA 12.4, PyTorch 2.6.0+cu124.

## What prompted it

Profiling the training loop produced this as the highest-severity finding:

```
NEVER_USED  --  object 9223372036854775848
size 20.0 MB | idle 172 kernels | severity 32.1%
   allocated, and no ACCESS, SET, COPYT or COPYF ever recorded
```

20 MB allocated and never touched is a large claim for a model whose parameters
total 2.51 MiB. `hpcprof` would normally attribute it to a call path, but it
runs for tens of minutes on a Python process and had not finished, so the
workload was bisected instead.

## Method

Six scripts, each adding one operation, profiled separately
(`run_bisect.sh`), producing profiles `s1_context/` … `s6_optim/` that can be
re-analyzed with no GPU:

```bash
python main.py analyze <profile_dir> --min-bytes 1
```

> The captured profiles for this case study were in `findings/pytorch-allocator-segments/`
> and were dropped when this tree was pruned to the core skill (see `PROVENANCE.md`);
> re-clone upstream if you need them. The numbers below are the measured result.

| Stage | Objects | Peak | New this stage |
|---|---:|---:|---|
| CUDA context only | 1 | 2.0 MB | 2.0 MB |
| **+ `model.to(device)`** | 2 | **22.0 MB** | **20.0 MB** |
| + input tensors | 2 | 22.0 MB | (none) |
| + forward pass | 6 | 32.1 MB | 8.0 MB, 2.0 MB, 128 KB, 1 KB |
| + backward | 10 | 60.3 MB | 20.0 MB, 8.0 MB, 128 KB, 1 KB |
| + `optimizer.step()` | 10 | 60.3 MB | (none) |

The 20 MB appears at `model.to(device)` — moving 2.51 MiB of parameters.

## What the sizes are

| Bytes | | Ops recorded | What it is |
|---:|---:|---|---|
| 20,971,520 | 20 MiB | ALLOC, COPYT | PyTorch `kLargeBuffer` large-pool segment |
| 20,971,520 | 20 MiB | ALLOC | `kLargeBuffer` large-pool segment |
| 8,388,608 | 8 MiB | ALLOC | rounded large-pool segment |
| 2,097,152 | 2 MiB | ALLOC, COPYT×4 | PyTorch `kSmallBuffer` small-pool segment |

**Six of ten objects are exact multiples of 2 MiB**, and the two 20 MB objects
are exactly `20971520` — PyTorch's `kLargeBuffer` constant. These are the sizes
the caching allocator requests from `cudaMalloc`; they are not tensor sizes.

## Why the objects look "never used"

PyTorch calls `cudaMalloc` once per *segment*, then sub-allocates tensors inside
it without further CUDA allocation calls. DrGPUM's `memory_liveness` intercepts
the CUDA allocation API, so it sees the segment. Kernels then read and write
*offsets inside* that segment, which DrGPUM cannot attribute back to the parent
allocation unless it is built with `REDSHOW_TORCH_SUBMEMORY_ANALYSIS` — the
`torch-monitor` component, which emits the `SUBALLOC` / `SUBFREE` operations
and `torch_python_states.txt`.

This build skipped `torch-monitor` (it needs `PYTORCH_DIR` and a spack Python
3.8 while this system's torch is on Python 3.10). Consequences:

- Segments show `ALLOC` with no `ACCESS` → reported `NEVER_USED`
- Segments are held until process exit by design → reported `NEVER_FREED`
- Both rank at the top by severity, because they are the largest objects

## Consequences for the project

1. **`torch-monitor` is required, not optional, for PyTorch work.** Without it
   the profiler cannot see below the allocator, and its findings describe
   allocator behaviour rather than the application. It was deprioritised to
   unblock the build; this finding reverses that.
2. **The parser is already prepared.** `SUBALLOC` (6) and `SUBFREE` (7) are in
   `OP_NAMES` and `MemoryObject.alloc_op`/`free_op` already accept them, so
   sub-allocation traces will parse without changes.
3. **`NEVER_FREED` guidance was right for the wrong reason.** The detector
   warns that torch holds blocks until exit by design. That is true, and it is
   also the *only* thing being observed here.
4. **The workload is not leaking.** A separate check — 5 iterations vs 150 —
   produced identical object ids, sizes, and peak (62.3 MB). Nothing
   accumulates across iterations, which independently rules out a real leak.

## Reproducing

```bash
# on a machine with DrGPUM installed
cd bisect && ./run_bisect.sh
python3 summarize.py     # the stage table
python3 sizes.py         # the size breakdown
```


---

# Resolution: torch-monitor built, and a second knob that matters more

`torch-monitor` was subsequently built against torch 2.6.0 / Python 3.10 (not
the spack Python 3.8 the install script assumes) and it compiled clean —
`c10::MemoryReportingInfoBase` is unchanged in torch 2.6, and torch-monitor
already carries `#if TORCH_VERSION_MAJOR >= 2` handling.

Profiling then requires **two** control knobs, both off by default:

```bash
gvprof -v -ck HPCRUN_SANITIZER_LIVENESS_ONGPU=1           -ck HPCRUN_SANITIZER_TORCH_ANALYSIS=1           -e memory_liveness python3 train.py
```

## What each knob changes

`TORCH_ANALYSIS=1` adds seven files — `submemory_liveness.txt`,
`submemory_size_list.txt`, `submemory_info.txt`,
`submemory_growth_sequence.txt`, `torch_python_states.txt`,
`merged_torch_python_states.txt`, `torch_libunwind_backtrace.txt` — exposing
**180 tensors** inside the 11 allocator segments, each with a Python stack.

`LIVENESS_ONGPU=1` turns out to matter more. Without it **no `ACCESS`
operation is recorded anywhere in the trace**, which has two consequences:

| | ONGPU off | ONGPU on |
|---|---|---|
| ACCESS ops recorded | **0** | 319 |
| `optimal_memory_peak` | **0 — "not computed"** | **44.0 MB** |
| Recoverable vs 62.3 MB peak | unknown | **18.3 MB (29.3%)** |
| `memory_peak_kernel` | 0 | a real kernel |
| `NEVER_USED` findings | 6 (all suspect) | 4 |

With no ACCESS data every object is trivially "never accessed", so
`NEVER_USED` fires on everything — 86 findings on the tensor view. And DrGPUM's
headline metric, `optimal_memory_peak`, is simply absent.

**Every profile taken earlier in this project was missing both.** The
detectors now refuse to report `NEVER_USED` when
`Profile.access_tracking_available` is false, and `benchmark_runner` passes both
knobs on every run.

## Remaining limitation

`ACCESS` is attributed to the parent **segment**, never to the sub-allocation —
even with both knobs, `submemory_liveness.txt` contains only `SUBALLOC`,
`SUBFREE`, and `COPYT`. So at tensor granularity:

- **usable**: allocation site (Python file and line), size, lifetime
  (SUBALLOC → SUBFREE), and therefore `DELAYED_FREE` / `LATE_FIRST_USE`
- **not usable**: `NEVER_USED`, which needs ACCESS

`Profile.submemory_view()` runs the detectors at tensor granularity and the
access guard suppresses the usage-based ones automatically.

## What tensor-level attribution gives you

Allocations by application source line, from `torch_python_states.txt`:

| Line | Code | Allocations |
|---|---|---|
| 32 | `optimizer.zero_grad(); loss.backward(); optimizer.step()` | 158 |
| 31 | `loss = criterion(model(x), y)` | 135 |
| 23 | `return self.fc3(self.relu2(...))` — forward | 101 |
| 30 | `y = torch.randint(...)` | 14 |
| 29 | `x = torch.randn(...)` | 14 |
| 25 | `model = SimpleNet().to(device)` | 6 |

Line 25 is where the bisection independently located the 20 MB segment, so the
two methods agree.
