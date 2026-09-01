"""
Memory-inefficiency detectors over a parsed DrGPUM profile.

Each detector answers the three questions the project has to answer for every
finding it reports:

  1. What is inefficient        -> Finding.kind + Finding.evidence
  2. Why that is harmful        -> Finding.wasted_bytes / idle_byte_kernels
  3. How to fix it              -> Finding.fix

Quantifying harm
----------------
DrGPUM records no timestamps. `op_id` is a monotonic counter, so it orders
events but its magnitude means nothing -- an op_id gap of 5000 may be a
microsecond or a second. Kernel launches are the one countable unit of progress
in the trace, so idle spans are measured in *kernel launches*, and harm in
**idle byte-kernels**: bytes resident x kernels launched while those bytes went
untouched. It is the discrete analogue of the memory-x-time integral that
DrGPUM's own `optimal_memory_peak` is computed against.

That number is comparable across objects within one run. It is NOT comparable
across runs with different kernel counts, so `severity` normalises it against
the run's total byte-kernel budget (peak bytes x total kernels).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .parser import Profile, MemoryObject, human_bytes

# A gap smaller than this many kernel launches is noise, not a finding.
DEFAULT_MIN_IDLE_KERNELS = 8

# Ignore objects below this size; freeing a 4 KB buffer earlier helps nobody.
DEFAULT_MIN_BYTES = 64 * 1024


@dataclass
class Finding:
    """One reported inefficiency."""

    kind: str
    object_id: Optional[int]
    size_bytes: int
    idle_kernels: int
    idle_byte_kernels: int
    severity: float                      # 0..1 share of the run's byte-kernel budget
    evidence: str
    harm: str
    fix: str
    detail: Dict = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"[{self.kind}] object {self.object_id} "
            f"({human_bytes(self.size_bytes)}, idle {self.idle_kernels} kernels, "
            f"severity {self.severity:.1%})"
        )

    def to_dict(self) -> Dict:
        return {
            "kind": self.kind,
            "object_id": self.object_id,
            "size_bytes": self.size_bytes,
            "size_human": human_bytes(self.size_bytes),
            "idle_kernels": self.idle_kernels,
            "idle_byte_kernels": self.idle_byte_kernels,
            "severity": round(self.severity, 6),
            "evidence": self.evidence,
            "harm": self.harm,
            "fix": self.fix,
            **({"detail": self.detail} if self.detail else {}),
        }


def _budget(profile: Profile) -> int:
    """Total byte-kernels the run could possibly waste. Used to normalise."""
    kernels = len(profile.kernel_ops)
    peak = profile.current_memory_peak or profile.total_allocated_bytes()
    return max(1, peak * max(1, kernels))


def _severity(profile: Profile, idle_byte_kernels: int) -> float:
    return min(1.0, idle_byte_kernels / _budget(profile))


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------

def detect_never_used(
    profile: Profile, min_bytes: int = DEFAULT_MIN_BYTES
) -> List[Finding]:
    """Memory that was allocated and then never read, written, or copied.

    The strongest finding available: the allocation is pure waste and can be
    deleted outright with no change to program semantics.
    """
    # Without ACCESS data every object is trivially "never used". Reporting
    # that would be 86 false findings on a real PyTorch run -- measured.
    if not profile.access_tracking_available:
        return []

    findings = []
    for obj in profile.objects.values():
        if not obj.is_never_used or (obj.size_bytes or 0) < min_bytes:
            continue
        alloc = obj.alloc_op
        free = obj.free_op
        idle = (
            profile.kernels_between(alloc, free)
            if alloc is not None and free is not None
            else len(profile.kernel_ops)
        )
        size = obj.size_bytes or 0
        ibk = size * idle
        findings.append(
            Finding(
                kind="NEVER_USED",
                object_id=obj.object_id,
                size_bytes=size,
                idle_kernels=idle,
                idle_byte_kernels=ibk,
                severity=_severity(profile, ibk),
                evidence=(
                    f"Object {obj.object_id} ({human_bytes(size)}) was allocated at "
                    f"op {alloc} and "
                    + (f"freed at op {free}" if free is not None else "never freed")
                    + ", with no ACCESS, SET, COPYT or COPYF recorded in between."
                ),
                harm=(
                    f"{human_bytes(size)} of device memory is reserved across "
                    f"{idle} kernel launches and never read or written. It "
                    f"contributes to peak memory and to allocator fragmentation "
                    f"while contributing nothing to the result."
                ),
                fix=(
                    "Delete the allocation. If it is a preallocated scratch or "
                    "workspace buffer kept 'just in case', confirm no code path "
                    "uses it and remove it; if a rarely-taken branch needs it, "
                    "allocate lazily inside that branch."
                ),
            )
        )
    return findings


def detect_late_first_use(
    profile: Profile,
    min_idle_kernels: int = DEFAULT_MIN_IDLE_KERNELS,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> List[Finding]:
    """Memory allocated well before anything touches it.

    The allocation could be sunk to the point of first use, lowering peak memory
    for the whole preceding span.
    """
    findings = []
    for obj in profile.objects.values():
        alloc, first_use = obj.alloc_op, obj.first_use_op
        size = obj.size_bytes or 0
        if alloc is None or first_use is None or size < min_bytes:
            continue
        idle = profile.kernels_between(alloc, first_use)
        if idle < min_idle_kernels:
            continue
        ibk = size * idle
        findings.append(
            Finding(
                kind="LATE_FIRST_USE",
                object_id=obj.object_id,
                size_bytes=size,
                idle_kernels=idle,
                idle_byte_kernels=ibk,
                severity=_severity(profile, ibk),
                evidence=(
                    f"Object {obj.object_id} ({human_bytes(size)}) was allocated at "
                    f"op {alloc} but first touched at op {first_use} -- "
                    f"{idle} kernel launches later."
                ),
                harm=(
                    f"For {idle} kernel launches the run carries "
                    f"{human_bytes(size)} it cannot use. If peak memory occurs in "
                    f"that window, this allocation is inflating it directly and "
                    f"capping the batch size or model size that fits."
                ),
                fix=(
                    "Move the allocation down to just before first use. In PyTorch "
                    "that usually means constructing the tensor inside the loop or "
                    "branch that consumes it rather than hoisting it to setup, or "
                    "replacing an eager `torch.empty`/`torch.zeros` with lazy "
                    "creation on first access."
                ),
                detail={"alloc_op": alloc, "first_use_op": first_use},
            )
        )
    return findings


def detect_delayed_free(
    profile: Profile,
    min_idle_kernels: int = DEFAULT_MIN_IDLE_KERNELS,
    min_bytes: int = DEFAULT_MIN_BYTES,
) -> List[Finding]:
    """Memory kept alive long after its last use.

    Symmetric to LATE_FIRST_USE and usually the easier fix: the object is dead
    but still resident, so freeing at last use is semantics-preserving.
    """
    findings = []
    for obj in profile.objects.values():
        last_use, free = obj.last_use_op, obj.free_op
        size = obj.size_bytes or 0
        if last_use is None or free is None or size < min_bytes:
            continue
        idle = profile.kernels_between(last_use, free)
        if idle < min_idle_kernels:
            continue
        ibk = size * idle
        findings.append(
            Finding(
                kind="DELAYED_FREE",
                object_id=obj.object_id,
                size_bytes=size,
                idle_kernels=idle,
                idle_byte_kernels=ibk,
                severity=_severity(profile, ibk),
                evidence=(
                    f"Object {obj.object_id} ({human_bytes(size)}) was last used at "
                    f"op {last_use} but not freed until op {free} -- "
                    f"{idle} kernel launches of dead residency."
                ),
                harm=(
                    f"{human_bytes(size)} stays resident across {idle} kernel "
                    f"launches after its final use. This is the classic driver of "
                    f"the gap between actual and optimal peak memory: the bytes are "
                    f"unreachable but still counted."
                ),
                fix=(
                    "Free at last use. In PyTorch, `del tensor` immediately after "
                    "the final read (and dropping any stale reference held by a "
                    "list, closure, or `self.` attribute) lets the caching "
                    "allocator reuse the block. Narrowing the variable's scope "
                    "achieves the same thing more robustly."
                ),
                detail={"last_use_op": last_use, "free_op": free},
            )
        )
    return findings


def detect_never_freed(
    profile: Profile, min_bytes: int = DEFAULT_MIN_BYTES
) -> List[Finding]:
    """Allocations with no matching free -- held until process exit."""
    findings = []
    for obj in profile.objects.values():
        size = obj.size_bytes or 0
        if not obj.is_never_freed or size < min_bytes:
            continue
        last_use = obj.last_use_op
        idle = (
            profile.kernels_between(last_use, max(profile.kernel_op_ids, default=0))
            if last_use is not None
            else len(profile.kernel_ops)
        )
        ibk = size * idle
        findings.append(
            Finding(
                kind="NEVER_FREED",
                object_id=obj.object_id,
                size_bytes=size,
                idle_kernels=idle,
                idle_byte_kernels=ibk,
                severity=_severity(profile, ibk),
                evidence=(
                    f"Object {obj.object_id} ({human_bytes(size)}) has an ALLOC but "
                    f"no FREE anywhere in the trace"
                    + (f"; last used at op {last_use}." if last_use else "; never used.")
                ),
                harm=(
                    f"{human_bytes(size)} is held until process exit. In a "
                    f"long-running training loop this compounds every iteration "
                    f"and is a common cause of out-of-memory failures that appear "
                    f"only after many steps."
                ),
                fix=(
                    "In a PyTorch process, check this against the caching "
                    "allocator FIRST: torch holds device blocks until process "
                    "exit by design, so most NEVER_FREED objects in a torch run "
                    "are the allocator working correctly, not a leak. Treat it as "
                    "actionable only if the object grows across iterations or is "
                    "large and never reused. For a genuine leak, look for tensors "
                    "accumulating in a Python list or on a module attribute -- "
                    "appending a tensor that still carries its autograd graph is "
                    "the usual culprit; store `.detach()` or `.item()` instead. "
                    "In a raw CUDA program, add the missing cudaFree."
                ),
                detail={"last_use_op": last_use},
            )
        )
    return findings


def detect_redundant_h2d(
    profile: Profile, min_bytes: int = DEFAULT_MIN_BYTES
) -> List[Finding]:
    """Repeated host-to-device copies into the same object with no use between.

    Each COPYT that is overwritten by another COPYT before anything reads the
    buffer moved bytes across PCIe for nothing.
    """
    findings = []
    for obj in profile.objects.values():
        size = obj.size_bytes or 0
        if size < min_bytes:
            continue

        wasted, pairs, pending = 0, [], None
        for op in obj.ops:
            if op.op == "COPYT":
                if pending is not None:
                    wasted += 1
                    pairs.append((pending, op.op_id))
                pending = op.op_id
            elif op.op in ("ACCESS", "COPYF"):
                pending = None

        if not wasted:
            continue

        ibk = size * wasted
        findings.append(
            Finding(
                kind="REDUNDANT_H2D_COPY",
                object_id=obj.object_id,
                size_bytes=size,
                idle_kernels=wasted,
                idle_byte_kernels=ibk,
                severity=_severity(profile, ibk),
                evidence=(
                    f"Object {obj.object_id} ({human_bytes(size)}) received "
                    f"{wasted} host-to-device transfer(s) that were overwritten by a "
                    f"later transfer before any kernel read them. "
                    f"Overwritten pairs (op_id -> op_id): {pairs[:5]}"
                    + (" ..." if len(pairs) > 5 else "")
                ),
                harm=(
                    f"{human_bytes(size * wasted)} crossed PCIe with no consumer. "
                    f"Host-to-device bandwidth is roughly an order of magnitude "
                    f"below device memory bandwidth, and a synchronous copy stalls "
                    f"the launch queue, so this costs wall-clock time as well as "
                    f"bandwidth."
                ),
                fix=(
                    "Hoist the transfer out of the loop if the data is loop-"
                    "invariant, or drop the dead one entirely. If the data really "
                    "does change per iteration, overlap it: pinned host memory plus "
                    "`.to(device, non_blocking=True)` on a side stream."
                ),
                detail={"redundant_transfers": wasted, "pairs": pairs[:20]},
            )
        )
    return findings


def detect_peak_gap(
    profile: Profile, min_bytes: int = DEFAULT_MIN_BYTES
) -> List[Finding]:
    """DrGPUM's headline number: actual peak vs achievable peak.

    This is a whole-run finding rather than a per-object one, and it bounds how
    much the per-object findings can possibly recover.

    Suppressed on runs whose peak underflowed the profiler's own u64 arithmetic
    (real peak under 512 B) or whose peak is below `min_bytes`. Such a run is a
    smoke test, not a workload -- reporting "100% of peak is recoverable" from
    12 bytes would be technically true and completely useless.
    """
    gap = profile.peak_gap_bytes
    if gap is None or gap <= 0 or profile.peak_underflowed:
        return []
    if not profile.optimal_peak_available:
        return []
    if (profile.current_memory_peak or 0) < min_bytes:
        return []
    fraction = profile.peak_waste_fraction or 0.0
    return [
        Finding(
            kind="PEAK_MEMORY_GAP",
            object_id=None,
            size_bytes=gap,
            idle_kernels=len(profile.kernel_ops),
            idle_byte_kernels=gap * max(1, len(profile.kernel_ops)),
            severity=min(1.0, fraction),
            evidence=(
                f"DrGPUM reports current_memory_peak = "
                f"{human_bytes(profile.current_memory_peak)} against "
                f"optimal_memory_peak = {human_bytes(profile.optimal_memory_peak)}, "
                f"at kernel {profile.memory_peak_kernel}."
            ),
            harm=(
                f"{human_bytes(gap)} ({fraction:.1%} of peak) is held at the high-"
                f"water mark without being needed there. That headroom is what "
                f"determines whether a larger batch, longer sequence, or bigger "
                f"model fits on the device."
            ),
            fix=(
                "This is the budget the per-object findings draw down. Fix the "
                "highest-severity DELAYED_FREE and LATE_FIRST_USE objects that are "
                "live at kernel "
                f"{profile.memory_peak_kernel} first -- only allocations live at "
                "the peak can lower the peak."
            ),
            detail={
                "current_memory_peak": profile.current_memory_peak,
                "optimal_memory_peak": profile.optimal_memory_peak,
                "peak_kernel": profile.memory_peak_kernel,
            },
        )
    ]


ALL_DETECTORS = (
    detect_peak_gap,
    detect_never_used,
    detect_never_freed,
    detect_delayed_free,
    detect_late_first_use,
    detect_redundant_h2d,
)


def analyze(profile: Profile, **kwargs) -> List[Finding]:
    """Run every detector and return findings ranked by severity.

    kwargs are forwarded to the detectors that accept them (`min_bytes`,
    `min_idle_kernels`).
    """
    import inspect

    findings: List[Finding] = []
    for detector in ALL_DETECTORS:
        accepted = set(inspect.signature(detector).parameters) - {"profile"}
        findings.extend(detector(profile, **{k: v for k, v in kwargs.items() if k in accepted}))

    findings.sort(key=lambda f: f.severity, reverse=True)
    return findings
