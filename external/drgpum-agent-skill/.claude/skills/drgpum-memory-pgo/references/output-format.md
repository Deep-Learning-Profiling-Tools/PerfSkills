# DrGPUM `memory_liveness` output format

Every format here was derived from the profiler source —
`redshow/src/analysis/memory_liveness.cpp` (the `output_*` functions) and the
`memory_operation_t` enum in the matching header — then verified against a real
capture on an RTX 3090. It was not inferred from sample output.

Produced by:

```bash
gvprof -v -e memory_liveness ./your_app
```

into `gvprof-measurements/memory_liveness/`.

## Files

| File | Format |
|---|---|
| `memory_liveness.txt` | `[<object_id>]: <op_id>(<op_enum>), <op_id>(<op_enum>), ` |
| `memory_size_list.txt` | `op_id=<object_id>, size=<bytes>`, size-descending |
| `kernel_list.txt` | `<op_id>, <ctx_id>` — one line per kernel launch |
| `memory_liveness.csv` | peak header, then `op_id: <n>, <OP> <ctx_id>` — **not CSV** |
| `memory_liveness.csv.context` | `<ctx_id>  <OpName>  <n> count(s)` then call-stack frames |
| `memory_growth_sequence.txt` | peak header, then `<op_id>(<op>): <bytes> B` |
| `operation_stream_info.txt` | the trace again, annotated with CUDA stream ids |
| `dependency_graph.dot`, `topological_order.txt` | object dependency graph |

## Operation enum

`0 ALLOC · 1 SET · 2 COPYT (host→device) · 3 COPYF (device→host) · 4 ACCESS · 5 FREE · 6 SUBALLOC · 7 SUBFREE`

`SUBALLOC`/`SUBFREE` appear only when the profiler is built with
`REDSHOW_TORCH_SUBMEMORY_ANALYSIS` (the `torch-monitor` component). Without it,
PyTorch tensors are invisible — see `pytorch-allocator-segments.md`.

## The peak header

```
memory_peak_kernel: 214
optimal_memory_peak: 402653184 B
current_memory_peak: 1140850688 B
```

`current − optimal` is the recoverable memory, and it bounds everything the
skill can achieve on a run.

## Traps

**`memory_liveness.csv` is not a CSV.** It contains `op_id: 214, ALLOC 1000`
text lines. Parsing it as columnar data — and then inventing plausible column
names for a GPU profiler — is exactly how this project ended up with a
fabricated `kernel_name,runtime_ms,sm_utilization_%,memory_bandwidth_%` schema
that no profiler ever produced. See `../../../MIGRATION.md`.

**`current_memory_peak` can underflow.** `output_ctx_node()` prints
`_current_memory_peak - 512` as unsigned 64-bit, so a run whose true peak is
below 512 B wraps to ~1.8×10¹⁹. The upstream vectorAdd sample reports
`18446744073709551116` for a real peak of 12 B. Unwrap with
`value - 2**64 + 512`.

**`optimal_memory_peak: 0` means "not computed"**, not "zero bytes needed" — it
appears alongside `memory_peak_kernel: 0` when the optimal-peak analysis did not
run. Treating it as a value produces a bogus "100% of peak is recoverable"
headline. Observed on a real PyTorch capture whose current peak was 62 MB.

**`op_id` starts at 2⁶³ + n.** Anything storing these as signed 64-bit breaks.

**There are no timestamps.** `op_id` orders events; its magnitude means nothing.
Kernel-launch count is the only usable clock.

## What DrGPUM does not record

No kernel runtime, no SM occupancy, no memory bandwidth, no FLOPs. Those come
from Nsight Compute. Any analysis quoting them from a DrGPUM run is fabricated.
