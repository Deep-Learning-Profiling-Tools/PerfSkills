"""Conservative screening diagnostics, separate from statistical confirmation.

Envelopes describe observed spread and reporting precision; they are NOT confidence
intervals. Profiled application times are diagnostic only and never rank candidates.
"""

from __future__ import annotations

import math
import statistics


def precision_envelope(base: list[dict], candidate: list[dict], direction: str,
                       sampling: dict) -> list[float]:
    """Median ratio bounds from printed precision only, not a statistical CI."""
    def quantum(rows):
        declared = sampling.get("metric_resolution")
        return declared if declared is not None else max((x.get("reporting_quantum") or 0 for x in rows), default=0)
    b, c = statistics.median(x["metric"] for x in base), statistics.median(x["metric"] for x in candidate)
    bq, cq = quantum(base), quantum(candidate)
    if direction == "higher":
        b, c, bq, cq = c, b, cq, bq
    return [max(0, b - bq) / max(1e-300, c + cq),
            (b + bq) / max(1e-300, c - cq)]


def quality(base: list[dict], candidate: list[dict], direction: str,
            sampling: dict) -> dict:
    declared = sampling.get("metric_resolution")
    quanta = [x.get("reporting_quantum") for x in base + candidate]
    quantum = declared if declared is not None else max((x for x in quanta if x), default=0)
    b = [x["metric"] for x in base]
    c = [x["metric"] for x in candidate]
    margin = sampling.get("screen_margin", .05)
    valid = all(math.isfinite(x) and x > 0 for x in b + c)
    if not valid:
        return {"status": "invalid", "requires_confirmation": True,
                "speedup_envelope": None, "envelope_kind": "observed-not-ci"}
    # A full output quantum covers truncation as well as rounding. A margin also
    # prevents a single sample from claiming precise separation from an incumbent.
    low_b, high_b = max(1e-300, min(b) - quantum), max(b) + quantum
    low_c, high_c = max(1e-300, min(c) - quantum), max(c) + quantum
    envelope = ([low_b / high_c, high_b / low_c] if direction == "lower"
                else [low_c / high_b, high_c / low_b])
    envelope = [envelope[0] / (1 + margin), envelope[1] * (1 + margin)]
    spread = max((max(v) - min(v)) / statistics.median(v) for v in (b, c))
    coarse = quantum > 0 and min(*b, *c) <= 20 * quantum
    status = ("unstable" if spread > margin else "resolution-limited" if coarse
              else "single-pair" if len(c) == 1 else "provisional")
    return {"status": status, "requires_confirmation": True,
            "reporting_quantum": quantum or None,
            "quantum_source": "declared" if declared is not None else "output-format" if quantum else None,
            "relative_spread": spread, "speedup_envelope": envelope,
            "envelope_kind": "observed-not-ci",
            "incumbent_at_screen": sampling.get("incumbent_speedup", 1.0)}


def bounds(row: dict) -> tuple[float, float]:
    envelope = (row.get("ranking") or {}).get("speedup_envelope")
    if envelope:
        return tuple(envelope)
    point = (row.get("improvement") or {}).get("point", 0)
    return point / 1.05, point * 1.05


def confirmation_shortlist(state: dict, limit: int = 2, direction: str = "lower") -> list[str]:
    """Keep an overlapping contender for independent confirmation; bounded cost."""
    rows = [(key, meta.get("result") or {}) for key, meta in state.get("candidates", {}).items()]
    rows = [(key, row) for key, row in rows if (row.get("correctness") or {}).get("passed")
            and (row.get("improvement") or {}).get("point", 0) > 0]
    measured = all(isinstance((row.get("metric") or {}).get("median"), (int, float)) for _, row in rows)
    if measured:
        rows.sort(key=lambda item: item[1]["metric"]["median"], reverse=direction == "higher")
    else:
        rows.sort(key=lambda item: item[1]["improvement"]["point"], reverse=True)
    if not rows:
        return []
    leader, leader_row = rows[0]
    cutoff = bounds(leader_row)[0]
    if measured:
        def interval(row):
            m = row["metric"]; return m.get("min", m["median"]) / 1.05, m.get("max", m["median"]) * 1.05
        lo, hi = interval(leader_row)
        contenders = [(k, r) for k, r in rows[1:] if (interval(r)[0] <= hi if direction == "lower" else interval(r)[1] >= lo)]
    else:
        contenders = [(key, row) for key, row in rows[1:] if bounds(row)[1] >= cutoff]
    # Retain the contender with the largest unresolved observed upside, not the
    # largest profiled time ratio. Stable insertion order resolves exact ties.
    contenders.sort(key=lambda item: bounds(item[1])[1], reverse=True)
    return [leader, *(key for key, _ in contenders[:max(0, limit - 1)])]


def diagnostic_disagreement(ranking: dict, metric: dict, profiled: float | None,
                            direction: str, sampling: dict) -> dict:
    """Record a mismatch without promoting the instrumented measurement."""
    if profiled is None or not math.isfinite(profiled) or profiled <= 0:
        return ranking
    low, high = metric.get("min"), metric.get("max")
    if low is None or high is None:
        return ranking
    padding = max(ranking.get("reporting_quantum") or 0,
                  metric["median"] * sampling.get("screen_margin", .05))
    if low - padding <= profiled <= high + padding:
        return ranking
    return ranking | {"diagnostic_disagreement": True,
                      "profiled_metric": profiled, "profiled_metric_role": "diagnostic-only",
                      "requires_confirmation": True}
