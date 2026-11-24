"""One cumulative deadline for a bounded evidence request, including its probes."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import math
from pathlib import Path
import time

from experiment_support import load

_deadline = ContextVar("profileweaver_acquisition_deadline", default=None)


class AcquisitionTimeout(RuntimeError):
    pass


def limited(plan):
    return (plan.get("sampling") or {}).get("measurement_policy") == "single-or-two-v3"


def seconds(plan, run=None):
    run = Path(run or plan.get("run_dir", "."))
    wall = (load(run / "baseline-timing.json", {}).get("sample") or {}).get("wall_s")
    explicit = (plan.get("profiling") or {}).get("acquisition_budget_s")
    value = explicit if explicit is not None else max(30, 3 * wall) if isinstance(wall, (int, float)) else 30
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("acquisition_budget_s must be a positive finite number")
    return min(value, (plan.get("execution") or {}).get("timeout_s") or value)


def remaining(requested=None):
    deadline = _deadline.get()
    if deadline is None:
        return requested
    left = deadline - time.monotonic()
    if left <= 0:
        raise AcquisitionTimeout("profiling acquisition budget exhausted; completed benchmark timing is preserved")
    return min(left, requested) if requested is not None else left


@contextmanager
def scope(plan, run=None):
    if not limited(plan):
        yield
        return
    old = _deadline.get()
    deadline = time.monotonic() + seconds(plan, run)
    token = _deadline.set(min(old, deadline) if old is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def bounded(function):
    @wraps(function)
    def invoke(plan, *args, **kwargs):
        run = args[0] if args and isinstance(args[0], Path) else None
        with scope(plan, run):
            result = function(plan, *args, **kwargs)
            remaining()
            return result
    return invoke
