"""
Parser for DrGPUM `memory_liveness` output.

DrGPUM (redshow) writes a directory of plain-text files, NOT a kernel-timing CSV.
Every format below was derived from the profiler source rather than guessed:
`redshow/src/analysis/memory_liveness.cpp` (the `output_*` functions) and the
`memory_operation_t` enum in `redshow/include/analysis/memory_liveness.h`.

Run the profiler as:

    gvprof -v -e memory_liveness ./your_app

which produces, in the output directory:

    memory_liveness.txt          per-object operation list
    memory_size_list.txt         object sizes, sorted descending
    kernel_list.txt              kernel launch op_ids -> context ids
    memory_liveness.csv          op trace + peak summary  (NOT csv, see below)
    memory_growth_sequence.txt   allocation size timeline + peak summary
    operation_stream_info.txt    same trace, annotated with CUDA stream ids

`memory_liveness.csv` carries a .csv extension but contains `op_id: N, ALLOC 12`
text lines. Parsing it as comma-separated data is what produced the earlier
fictional `kernel_name,runtime_ms,sm_utilization_%,memory_bandwidth_%` schema.
It has no such columns and DrGPUM reports no SM-utilization or bandwidth metric
at all -- it is a memory profiler.

`op_id` is a globally increasing counter, so ordering by op_id recovers program
order. That ordering is the only clock DrGPUM gives us; there are no timestamps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# redshow/include/analysis/memory_liveness.h:110-120
OP_NAMES: Dict[int, str] = {
    0: "ALLOC",
    1: "SET",
    2: "COPYT",   # host -> device
    3: "COPYF",   # device -> host
    4: "ACCESS",  # touched by a kernel
    5: "FREE",
    6: "SUBALLOC",  # PyTorch caching-allocator sub-block
    7: "SUBFREE",
}

#: Operations that count as the memory actually being used for something.
USE_OPS = frozenset({"SET", "COPYT", "COPYF", "ACCESS"})

#: Operations that read or write real data through a kernel.
KERNEL_USE_OPS = frozenset({"ACCESS"})


class DrGPUMParseError(Exception):
    """Raised when a DrGPUM output directory is unusable."""


@dataclass
class MemoryOp:
    """One recorded operation against one memory object."""

    op_id: int
    op: str

    @property
    def is_use(self) -> bool:
        return self.op in USE_OPS


@dataclass
class MemoryObject:
    """A single GPU allocation and everything that happened to it.

    `object_id` is the op_id of the allocation itself, which DrGPUM uses as the
    object's identity across all of its output files.
    """

    object_id: int
    size_bytes: Optional[int] = None
    ops: List[MemoryOp] = field(default_factory=list)

    def _first(self, names) -> Optional[int]:
        return next((o.op_id for o in self.ops if o.op in names), None)

    def _last(self, names) -> Optional[int]:
        return next((o.op_id for o in reversed(self.ops) if o.op in names), None)

    @property
    def alloc_op(self) -> Optional[int]:
        return self._first({"ALLOC", "SUBALLOC"})

    @property
    def free_op(self) -> Optional[int]:
        return self._last({"FREE", "SUBFREE"})

    @property
    def first_use_op(self) -> Optional[int]:
        return self._first(USE_OPS)

    @property
    def last_use_op(self) -> Optional[int]:
        return self._last(USE_OPS)

    @property
    def is_never_used(self) -> bool:
        """Allocated but no kernel or transfer ever touched it."""
        return self.first_use_op is None

    @property
    def is_never_freed(self) -> bool:
        return self.free_op is None

    def ops_of(self, name: str) -> List[MemoryOp]:
        return [o for o in self.ops if o.op == name]

    def __str__(self) -> str:
        size = "unknown size" if self.size_bytes is None else _human_bytes(self.size_bytes)
        return f"MemoryObject({self.object_id}, {size}, {len(self.ops)} ops)"


@dataclass
class PythonFrame:
    """One Python stack frame from `torch_python_states.txt`.

    Only produced when the profiler is built with torch-monitor and run with
    `-ck HPCRUN_SANITIZER_TORCH_ANALYSIS=1`.
    """

    file: str
    function: str
    first_line: int
    call_line: int

    @property
    def is_application(self) -> bool:
        """True for the user's own code, false for torch/library internals.

        Frames are dominated by torch internals (`nn/modules/module.py`,
        `optim/adam.py`); the useful one is the outermost frame that belongs to
        the application being profiled.
        """
        f = self.file.replace("\\", "/")
        return not (
            "/site-packages/" in f
            or "/dist-packages/" in f
            or "/lib/python" in f
            or f.startswith("<")
        )

    def __str__(self) -> str:
        return f"{self.file}:{self.call_line} in {self.function}()"


@dataclass
class Profile:
    """A parsed DrGPUM memory_liveness run."""

    objects: Dict[int, MemoryObject] = field(default_factory=dict)
    kernel_ops: List[Tuple[int, int]] = field(default_factory=list)
    growth: List[Tuple[int, str, int]] = field(default_factory=list)
    current_memory_peak: Optional[int] = None
    optimal_memory_peak: Optional[int] = None
    memory_peak_kernel: Optional[int] = None
    source_dir: Optional[Path] = None
    #: True when current_memory_peak had to be unwrapped from a u64 underflow
    #: in the profiler (real peak below 512 B). Such a run is too small for
    #: the peak figures to mean anything.
    peak_underflowed: bool = False
    #: context id -> call stack, from memory_liveness.csv.context
    contexts: Dict[int, List[str]] = field(default_factory=dict)
    #: op_id -> context id, from memory_liveness.csv
    op_contexts: Dict[int, int] = field(default_factory=dict)

    # -- torch-monitor sub-allocations (PyTorch tensors) --------------------
    # Present only when built with torch-monitor and run with
    # `-ck HPCRUN_SANITIZER_TORCH_ANALYSIS=1`. Without these, the objects above
    # are PyTorch caching-allocator *segments*, not tensors -- see
    # references/pytorch-allocator-segments.md.

    #: sub-allocation id -> object. These are the actual tensors.
    submemory: Dict[int, MemoryObject] = field(default_factory=dict)
    #: op_id -> Python stack, outermost last, from torch_python_states.txt
    python_states: Dict[int, List[PythonFrame]] = field(default_factory=dict)
    current_submemory_peak: Optional[int] = None
    optimal_submemory_peak: Optional[int] = None
    submemory_peak_kernel: Optional[int] = None

    @property
    def access_tracking_available(self) -> bool:
        """Whether any ACCESS operation was recorded.

        DrGPUM records ACCESS only when run with
        `-ck HPCRUN_SANITIZER_LIVENESS_ONGPU=1`. Without it the trace has no
        ACCESS at all, so "never accessed" is indistinguishable from "access
        not measured" -- and every object looks unused. Detectors that depend
        on usage must check this before reporting anything.

        Sub-allocations never carry ACCESS even with the knob set: it is
        attributed to the parent segment, not the tensor.
        """
        return any(
            op.op == "ACCESS" for obj in self.objects.values() for op in obj.ops
        )

    @property
    def has_submemory(self) -> bool:
        """Whether tensor-level data is available for this run."""
        return bool(self.submemory)

    def python_site(self, op_id: int) -> Optional[PythonFrame]:
        """The application source line responsible for an allocation.

        Returns the outermost frame belonging to the user's own code, skipping
        torch internals. This is what turns an object id into a line number.
        """
        frames = self.python_states.get(op_id) or []
        app = [f for f in frames if f.is_application]
        return app[-1] if app else (frames[-1] if frames else None)

    def submemory_view(self) -> "Profile":
        """A Profile whose `objects` are the sub-allocations.

        Detectors read `objects`, `kernel_ops`, and the peak fields, so this
        lets the same six detectors run at tensor granularity without
        duplicating them.
        """
        return Profile(
            objects=self.submemory,
            kernel_ops=self.kernel_ops,
            current_memory_peak=self.current_submemory_peak,
            optimal_memory_peak=self.optimal_submemory_peak,
            memory_peak_kernel=self.submemory_peak_kernel,
            source_dir=self.source_dir,
            contexts=self.contexts,
            op_contexts=self.op_contexts,
            python_states=self.python_states,
        )

    # -- headline numbers ---------------------------------------------------

    @property
    def optimal_peak_available(self) -> bool:
        """Whether DrGPUM actually computed an optimal peak for this run.

        It reports `optimal_memory_peak: 0` (alongside `memory_peak_kernel: 0`)
        when the optimal-peak analysis did not run -- observed on a real PyTorch
        capture whose current peak was 62 MB. Zero is "not computed", not "zero
        bytes needed": treating it as a real value turns every such run into a
        bogus "100% of peak is recoverable" headline.
        """
        return bool(self.optimal_memory_peak) and bool(self.current_memory_peak)

    @property
    def peak_gap_bytes(self) -> Optional[int]:
        """Bytes held at peak beyond what the program actually needed.

        This is DrGPUM's central claim: `optimal_memory_peak` is the peak the
        application would have hit had every allocation been live only while it
        was genuinely in use. The difference is recoverable memory.
        """
        if not self.optimal_peak_available:
            return None
        return self.current_memory_peak - self.optimal_memory_peak

    @property
    def peak_waste_fraction(self) -> Optional[float]:
        gap = self.peak_gap_bytes
        if gap is None or not self.current_memory_peak:
            return None
        return gap / self.current_memory_peak

    # -- the op_id timeline -------------------------------------------------

    @property
    def kernel_op_ids(self) -> List[int]:
        return sorted(op_id for op_id, _ in self.kernel_ops)

    def kernels_between(self, start_op: int, end_op: int) -> int:
        """How many kernels launched strictly between two op_ids.

        DrGPUM emits no timestamps, so kernel-launch count is the closest thing
        to a clock. It is what lets us say "this buffer sat idle across 400
        kernel launches" instead of an uninterpretable op_id delta.
        """
        if end_op <= start_op:
            return 0
        return sum(1 for k in self.kernel_op_ids if start_op < k < end_op)

    def call_stack_for(self, obj: "MemoryObject") -> List[str]:
        """Call stack at the allocation site, or [] if unavailable.

        Requires memory_liveness.csv.context, which gvprof writes alongside the
        trace. This is how a finding becomes a source location.
        """
        alloc = obj.alloc_op
        if alloc is None:
            return []
        ctx = self.op_contexts.get(alloc)
        return self.contexts.get(ctx, []) if ctx is not None else []

    def total_allocated_bytes(self) -> int:
        return sum(o.size_bytes or 0 for o in self.objects.values())

    def sorted_objects(self) -> List[MemoryObject]:
        return sorted(
            self.objects.values(), key=lambda o: (o.size_bytes or 0), reverse=True
        )


# ---------------------------------------------------------------------------
# individual file parsers
# ---------------------------------------------------------------------------

_PEAK_RE = re.compile(r"^(\w+):\s*(\d+)\s*B?\s*$")
_LIVENESS_LINE_RE = re.compile(r"^\[(\d+)\]:\s*(.*)$")
_OP_PAIR_RE = re.compile(r"(\d+)\((\d+)\)")
_SIZE_RE = re.compile(r"^op_id=(\d+),\s*size=(\d+)\s*$")
_GROWTH_RE = re.compile(r"^(\d+)\((\w+)\):\s*(\d+)\s*B\s*$")
# SUBALLOC/SUBFREE are written without a space before the ctx id -- see
# output_ctx_node() in memory_liveness.cpp. The `\s*` is deliberate.
_TRACE_RE = re.compile(r"^op_id:\s*(\d+),\s*([A-Z]+)\s*(-?\d+)\s*$")
# `95  Malloc  1 count(s)` in memory_liveness.csv.context
_CTX_HEADER_RE = re.compile(r"^(\d+)\s+(\w+)\s+\d+\s+count\(s\)\s*$")
# torch_python_states.txt: "ALLOC: 9223372036854775811" and "(0)File: /path"
_PY_RECORD_RE = re.compile(r"^(ALLOC|ACCESS|FREE|SUBALLOC|SUBFREE):\s*(\d+)\s*$")
_PY_FILE_RE = re.compile(r"^\((\d+)\)File:\s*(.+)$")


def _read(path: Path) -> Optional[List[str]]:
    if not path.is_file():
        return None
    return path.read_text(errors="replace").splitlines()


#: 2**64 -- DrGPUM writes u64 values, which can wrap. See below.
_U64 = 1 << 64


def _parse_peak_header(lines: List[str], profile: Profile) -> None:
    """Both memory_liveness.csv and memory_growth_sequence.txt start with these."""
    for line in lines[:6]:
        m = _PEAK_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), int(m.group(2))
        if key == "memory_peak_kernel":
            profile.memory_peak_kernel = value
        elif key == "optimal_memory_peak":
            profile.optimal_memory_peak = value
        elif key == "current_memory_peak":
            # DrGPUM prints `_current_memory_peak - 512` as an unsigned 64-bit
            # value (output_ctx_node in memory_liveness.cpp). When the true peak
            # is below 512 B that subtraction underflows and wraps to ~1.8e19 --
            # observed on the upstream vectorAdd sample, whose real peak is 12 B.
            # Unwrap it rather than reporting 16 exabytes.
            if value >= (1 << 63):
                value = value - _U64 + 512
                profile.peak_underflowed = True
            profile.current_memory_peak = value


def parse_memory_liveness_txt(lines: List[str], profile: Profile) -> None:
    """`[<object_id>]: <op_id>(<op_enum>), <op_id>(<op_enum>), `"""
    for line in lines:
        m = _LIVENESS_LINE_RE.match(line.strip())
        if not m:
            continue
        object_id = int(m.group(1))
        obj = profile.objects.setdefault(object_id, MemoryObject(object_id=object_id))
        for op_id_s, op_enum_s in _OP_PAIR_RE.findall(m.group(2)):
            op_enum = int(op_enum_s)
            obj.ops.append(
                MemoryOp(op_id=int(op_id_s), op=OP_NAMES.get(op_enum, f"UNKNOWN_{op_enum}"))
            )
        obj.ops.sort(key=lambda o: o.op_id)


def parse_memory_size_list(lines: List[str], profile: Profile) -> None:
    """`op_id=<object_id>, size=<bytes>`"""
    for line in lines:
        m = _SIZE_RE.match(line.strip())
        if not m:
            continue
        object_id, size = int(m.group(1)), int(m.group(2))
        obj = profile.objects.setdefault(object_id, MemoryObject(object_id=object_id))
        obj.size_bytes = size


def parse_kernel_list(lines: List[str], profile: Profile) -> None:
    """`<op_id>, <ctx_id>`"""
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            profile.kernel_ops.append((int(parts[0]), int(parts[1])))
        except ValueError:
            continue


def parse_growth_sequence(lines: List[str], profile: Profile) -> None:
    """`<op_id>(<op_name>): <bytes> B`, after the peak header."""
    _parse_peak_header(lines, profile)
    for line in lines:
        m = _GROWTH_RE.match(line.strip())
        if m:
            profile.growth.append((int(m.group(1)), m.group(2), int(m.group(3))))


def parse_op_trace(lines: List[str], profile: Profile) -> None:
    """`memory_liveness.csv`: peak header, then `op_id: <n>, <OP> <ctx_id>`.

    The per-op lines duplicate memory_liveness.txt, but they carry the *context
    id* that memory_liveness.txt does not -- which is the join key into
    memory_liveness.csv.context and therefore into a call stack.
    """
    _parse_peak_header(lines, profile)
    for line in lines:
        m = _TRACE_RE.match(line.strip())
        if m:
            profile.op_contexts[int(m.group(1))] = int(m.group(3))


def parse_context_file(lines: List[str], profile: Profile) -> None:
    """`memory_liveness.csv.context` -- call path per context id.

        95  Malloc  1 count(s)
        Unknown:0<TAB>main#
        Unknown:0<TAB>cudaMalloc#

    This is what lets a finding name a source location instead of just an
    object id. Frames are listed innermost-last in the file; we keep file order.
    """
    ctx_id: Optional[int] = None
    frames: List[str] = []

    def flush():
        if ctx_id is not None and frames:
            profile.contexts[ctx_id] = list(frames)

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush()
            ctx_id, frames = None, []
            continue
        header = _CTX_HEADER_RE.match(line)
        if header:
            flush()
            ctx_id, frames = int(header.group(1)), []
            continue
        if ctx_id is not None:
            frame = line.strip().rstrip("#").strip()
            if frame:
                frames.append(frame)
    flush()


def parse_submemory_liveness(lines: List[str], profile: Profile) -> None:
    """`submemory_liveness.txt` -- same shape as memory_liveness.txt.

    Sub-allocations use op enums 6 (SUBALLOC) and 7 (SUBFREE) where top-level
    objects use 0 (ALLOC) and 5 (FREE).
    """
    for line in lines:
        m = _LIVENESS_LINE_RE.match(line.strip())
        if not m:
            continue
        sub_id = int(m.group(1))
        obj = profile.submemory.setdefault(sub_id, MemoryObject(object_id=sub_id))
        for op_id_s, op_enum_s in _OP_PAIR_RE.findall(m.group(2)):
            op_enum = int(op_enum_s)
            obj.ops.append(
                MemoryOp(op_id=int(op_id_s), op=OP_NAMES.get(op_enum, f"UNKNOWN_{op_enum}"))
            )
        obj.ops.sort(key=lambda o: o.op_id)


def parse_submemory_size_list(lines: List[str], profile: Profile) -> None:
    """`submemory_size_list.txt` -- `op_id=<sub_id>, size=<bytes>`."""
    for line in lines:
        m = _SIZE_RE.match(line.strip())
        if not m:
            continue
        sub_id, size = int(m.group(1)), int(m.group(2))
        obj = profile.submemory.setdefault(sub_id, MemoryObject(object_id=sub_id))
        obj.size_bytes = size


def parse_submemory_info(lines: List[str], profile: Profile) -> None:
    """`submemory_info.txt` / `submemory_growth_sequence.txt` peak header.

    Same three keys as the top-level header but prefixed:
    `submemory_peak_kernel`, `optimal_submemory_peak`, `current_submemory_peak`.
    """
    for line in lines[:6]:
        m = _PEAK_RE.match(line.strip())
        if not m:
            continue
        key, value = m.group(1), int(m.group(2))
        if key == "submemory_peak_kernel":
            profile.submemory_peak_kernel = value
        elif key == "optimal_submemory_peak":
            profile.optimal_submemory_peak = value
        elif key == "current_submemory_peak":
            if value >= (1 << 63):          # same u64 underflow as the top level
                value = value - _U64 + 512
            profile.current_submemory_peak = value


def parse_torch_python_states(lines: List[str], profile: Profile) -> None:
    """`torch_python_states.txt` -- Python stack per allocation.

        ------------------------------
        ALLOC: 9223372036854775811
        (0)File: /path/to/torch/nn/modules/module.py
        <TAB>Function: convert
        <TAB>First line: 1320
        <TAB>Call at line: 1329
        (1)File: /path/to/train.py
        ...

    Records appear for ALLOC, ACCESS, and FREE; we key on op_id regardless of
    which, since the allocation site is what matters. Frames are listed
    innermost first, so the application frame is usually last.
    """
    op_id: Optional[int] = None
    frames: List[PythonFrame] = []
    cur: Dict[str, str] = {}

    def flush_frame():
        if cur.get("file"):
            try:
                frames.append(
                    PythonFrame(
                        file=cur["file"],
                        function=cur.get("function", "?"),
                        first_line=int(cur.get("first_line", 0)),
                        call_line=int(cur.get("call_line", 0)),
                    )
                )
            except ValueError:
                pass
        cur.clear()

    def flush_record():
        flush_frame()
        if op_id is not None and frames:
            profile.python_states.setdefault(op_id, list(frames))

    for raw in lines:
        line = raw.strip()
        if not line or set(line) == {"-"}:
            continue
        m = _PY_RECORD_RE.match(line)
        if m:
            flush_record()
            op_id, frames[:] = int(m.group(2)), []
            continue
        m = _PY_FILE_RE.match(line)
        if m:
            flush_frame()
            cur["file"] = m.group(2).strip()
            continue
        if line.startswith("Function:"):
            cur["function"] = line.split(":", 1)[1].strip()
        elif line.startswith("First line:"):
            cur["first_line"] = line.split(":", 1)[1].strip()
        elif line.startswith("Call at line:"):
            cur["call_line"] = line.split(":", 1)[1].strip()
    flush_record()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def parse_profile(output_dir) -> Profile:
    """Parse a DrGPUM `memory_liveness` output directory.

    Missing files are tolerated -- which files appear depends on whether
    REDSHOW_TORCH_SUBMEMORY_ANALYSIS was compiled in and whether the run
    produced any sub-allocations. At minimum `memory_liveness.txt` must exist.
    """
    path = Path(output_dir)
    if path.is_file():
        # Point at the directory, not one of the files inside it.
        path = path.parent
    if not path.is_dir():
        raise DrGPUMParseError(f"Not a directory: {output_dir}")

    profile = Profile(source_dir=path)

    liveness = _read(path / "memory_liveness.txt")
    if liveness is None:
        raise DrGPUMParseError(
            f"{path}/memory_liveness.txt not found. Point this at the directory "
            f"produced by `gvprof -e memory_liveness`, not at an ncu/nsys export."
        )
    parse_memory_liveness_txt(liveness, profile)

    for name, fn in (
        ("memory_size_list.txt", parse_memory_size_list),
        ("kernel_list.txt", parse_kernel_list),
        ("memory_growth_sequence.txt", parse_growth_sequence),
        ("memory_liveness.csv", parse_op_trace),
        ("memory_liveness.csv.context", parse_context_file),
        # torch-monitor outputs; absent unless built with it and enabled
        ("submemory_liveness.txt", parse_submemory_liveness),
        ("submemory_size_list.txt", parse_submemory_size_list),
        ("submemory_info.txt", parse_submemory_info),
        ("submemory_growth_sequence.txt", parse_submemory_info),
        ("torch_python_states.txt", parse_torch_python_states),
    ):
        lines = _read(path / name)
        if lines is not None:
            fn(lines, profile)

    if not profile.objects:
        raise DrGPUMParseError(f"No memory objects found in {path}")

    return profile


def _human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < step or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= step
    return f"{value:.1f} TB"


human_bytes = _human_bytes
