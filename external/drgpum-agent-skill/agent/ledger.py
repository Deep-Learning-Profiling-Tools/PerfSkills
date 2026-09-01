"""
The optimization ledger: an append-only record of what was tried.

This is the "Ledger" layer of the agent-memory design in .claude/skills/drgpum-memory-pgo/SKILL.md --
facts, add-only, never modified, trackable. Every optimization iteration writes
one row; nothing ever edits a previous one. It is the experimental record the
project's writeup is built from, and the thing the agent consults instead of
re-reading the whole conversation to answer "what have we already tried?".

Rejected attempts are as valuable as accepted ones. "We freed object X at its
last use and the loss diverged" is a finding: it means the finding was wrong
about that object's lifetime, and it stops the next iteration repeating it.

    from agent.ledger import Ledger

    led = Ledger("runs/ledger.jsonl")
    led.record(finding=f, action="del activations at last use",
               peak_before=65275392, peak_after=44040192, correctness="pass")
    print(led.format())
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .drgpum.parser import human_bytes


@dataclass
class Entry:
    """One optimization iteration. Immutable once written."""

    iteration: int
    kind: str                       # finding kind, e.g. DELAYED_FREE
    object_id: Optional[int]
    size_bytes: int
    action: str                     # what was actually changed in the source
    correctness: str                # "pass" | "fail" | "unchecked"
    peak_before: Optional[int] = None
    peak_after: Optional[int] = None
    note: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def peak_delta(self) -> Optional[int]:
        if self.peak_before is None or self.peak_after is None:
            return None
        return self.peak_before - self.peak_after

    @property
    def peak_delta_pct(self) -> Optional[float]:
        d = self.peak_delta
        if d is None or not self.peak_before:
            return None
        return d / self.peak_before * 100

    @property
    def accepted(self) -> bool:
        """Kept only if correctness passed AND peak actually fell."""
        return self.correctness == "pass" and (self.peak_delta or 0) > 0


class Ledger:
    """Append-only JSONL log. One line per iteration, never rewritten."""

    def __init__(self, path: str | Path = "runs/ledger.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __iter__(self) -> Iterator[Entry]:
        if not self.path.is_file():
            return iter(())
        entries = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                entries.append(Entry(**json.loads(line)))
        return iter(entries)

    def entries(self) -> List[Entry]:
        return list(self)

    @property
    def next_iteration(self) -> int:
        return len(self.entries()) + 1

    def record(
        self,
        kind: str,
        action: str,
        correctness: str,
        object_id: Optional[int] = None,
        size_bytes: int = 0,
        peak_before: Optional[int] = None,
        peak_after: Optional[int] = None,
        note: str = "",
    ) -> Entry:
        """Append one iteration. There is deliberately no update() or delete()."""
        if correctness not in ("pass", "fail", "unchecked"):
            raise ValueError(f"correctness must be pass/fail/unchecked, got {correctness!r}")
        entry = Entry(
            iteration=self.next_iteration,
            kind=kind,
            object_id=object_id,
            size_bytes=size_bytes,
            action=action,
            correctness=correctness,
            peak_before=peak_before,
            peak_after=peak_after,
            note=note,
        )
        with self.path.open("a") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")
        return entry

    # -- what the agent asks the ledger -------------------------------------

    def already_tried(self, object_id: int) -> List[Entry]:
        """Prior attempts on this object -- consult before proposing a fix."""
        return [e for e in self if e.object_id == object_id]

    def rejected_kinds(self) -> Dict[str, int]:
        """Finding kinds that failed correctness, and how often.

        A kind that keeps failing is a signal the detector is wrong about this
        codebase, not that the agent should keep trying.
        """
        out: Dict[str, int] = {}
        for e in self:
            if e.correctness == "fail":
                out[e.kind] = out.get(e.kind, 0) + 1
        return out

    def should_stop(self, min_gain_pct: float = 2.0, no_progress: int = 3) -> Optional[str]:
        """Check the SKILL.md termination clauses. Returns a reason, or None.

        Hitting one of these is a successful outcome, not a failure.
        """
        entries = self.entries()
        if not entries:
            return None
        last = entries[-1]
        if last.accepted and (last.peak_delta_pct or 0) < min_gain_pct:
            return (
                f"last accepted iteration gained "
                f"{last.peak_delta_pct:.1f}% -- below the {min_gain_pct}% noise floor"
            )
        recent = entries[-no_progress:]
        if len(recent) == no_progress and not any(e.accepted for e in recent):
            return f"{no_progress} consecutive iterations with no accepted fix"
        for kind, n in self.rejected_kinds().items():
            if n >= 2:
                return f"{kind} failed correctness {n} times -- the finding is wrong here"
        return None

    # -- rendering -----------------------------------------------------------

    def total_saved_bytes(self) -> int:
        return sum(e.peak_delta or 0 for e in self if e.accepted)

    def format(self) -> str:
        entries = self.entries()
        if not entries:
            return "Ledger is empty -- no optimization iterations recorded yet."

        rows = [
            "=" * 78,
            "OPTIMIZATION LEDGER  (append-only; rejected attempts are results too)",
            "=" * 78,
            "",
            f"{'#':>2}  {'kind':<20} {'size':>9}  {'peak delta':>12}  {'ok':<9} action",
            "-" * 78,
        ]
        for e in entries:
            delta = (
                f"{human_bytes(e.peak_delta)} ({e.peak_delta_pct:.1f}%)"
                if e.peak_delta is not None
                else "--"
            )
            mark = {"pass": "pass", "fail": "FAIL", "unchecked": "unchecked"}[e.correctness]
            rows.append(
                f"{e.iteration:>2}  {e.kind:<20} {human_bytes(e.size_bytes):>9}  "
                f"{delta:>12}  {mark:<9} {e.action[:28]}"
            )

        accepted = [e for e in entries if e.accepted]
        rows += [
            "-" * 78,
            f"  {len(accepted)}/{len(entries)} accepted; "
            f"total peak reduction {human_bytes(self.total_saved_bytes())}",
        ]
        stop = self.should_stop()
        if stop:
            rows += ["", f"  STOP: {stop}"]
        return "\n".join(rows)


def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m agent.ledger", description="Show the optimization ledger."
    )
    p.add_argument("path", nargs="?", default="runs/ledger.jsonl")
    args = p.parse_args(argv)
    print(Ledger(args.path).format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
