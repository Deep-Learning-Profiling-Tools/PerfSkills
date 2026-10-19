---
name: pw-nsight-systems
description: Profile NVIDIA GPU applications with Nsight Systems at host/device timeline granularity, then benchmark, reprofile, and verify a rewrite. Use for GPU gaps, kernel and CUDA API time, launches, transfers, synchronization, overlap, NVTX phases, and optional MPI/OpenMP/OpenACC/library traces. Not for occupancy, cache, bandwidth ceilings, registers, roofline, or instruction stalls.
---

# Nsight Systems child

Own `nsys` timeline evidence only. Declare `nsys --version` and the required help/capability probe
in cached preflight and reuse it; do not repeat successful probes. Never guess options. Preserve
the build, launcher, input, rank/GPU mapping, and correctness oracle.

Use the narrowest trace that answers the question. For CUDA/NVTX timing, start with:

```bash
nsys profile --trace=cuda,nvtx --sample=none --cpuctxsw=none --stats=false \
  --force-overwrite=true -o <capture> <command>
nsys export --type sqlite --force-overwrite=true -o <capture.sqlite> <capture.nsys-rep>
```

Adapt only after checking local help. Add OS runtime, MPI, UCX, NCCL, OpenMP, OpenACC, Python,
CUDA libraries, Unified Memory, GPU metrics, or backtraces only when the question requires them.
Read `references/tool-notes.md` for those modes or a tool failure; routine CUDA runs do not load
it.

Normalize the selected complete NVTX range:

```bash
python3 .claude/skills/pw-nsight-systems/scripts/reduce_sqlite.py \
  --sqlite <capture.sqlite> --run-id <run-id> --phase baseline \
  --capture-metadata <capture.json> --preflight <preflight.json> \
  --output <profile.json> --details-output <roi-metrics.json>
```

The reducer computes clipped GPU busy/idle unions, overlap, kernel distributions and launch
shapes, API idle overlap, timeline shape, ranked implications, and Amdahl ceilings. These facts
can localize a target; they cannot establish kernel-internal causes.

Cache collection and reduction separately. Consume `agent-brief.txt` and its hotspot slices.
Put candidate argv, metric, oracle, and capture/export/reduce/locate stages in the single
experiment contract. Let `shared/scripts/experiment.py` profile every correct candidate once,
cache its normalized timeline and delta, advance the source-bounded hotspot frontier, confirm, and
finalize. Confirmation reuses the iteration capture; the accepted winner receives a fresh final
reprofile. Escalate only a narrowed counter question that this timeline cannot answer.
