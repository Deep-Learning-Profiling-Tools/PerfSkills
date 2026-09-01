# TODO tool — mechanics and pitfalls

Reference loaded at stage 2. Keep it factual and version-stamped; this is where tool-specific
truth lives so `SKILL.md` can stay about method.

## Versions verified against

| Version | Date checked | Notes |
|---|---|---|
| TODO | TODO | TODO metric renames, flag changes, output format changes |

## Command recipes

TODO — one block per measurement mode, with *why each flag is there*.

```
# Mode: TODO (e.g. cheap first-pass sampling)
TODO command
```

```
# Mode: TODO (e.g. narrowed counter collection on one region)
TODO command
```

## Output format

TODO — where results land, how to get machine-readable output (CSV/JSON/protobuf), and the
post-processing command that produces it. Prefer the tool's own export over screen-scraping.

## Metric dictionary

Map the tool's names to the normalized schema. This table is what makes stage 3 honest.

| Tool metric | Schema field | Units | Definition / caveat |
|---|---|---|---|
| TODO | `hotspots[].self_time_s` | s | TODO exactly what it counts |
| TODO | `bottleneck.evidence[]` | — | TODO what value licenses which conclusion |

## Derived metrics

TODO — formulas this tool's users expect (e.g. arithmetic intensity, achieved bandwidth,
occupancy), with the exact counters they are built from and the denominator used. Write the
formula down; do not carry it in your head between runs.

## Pitfalls

TODO — the ways this tool lies or misleads. Typical entries:

- Sampling skid / instruction attribution offset near the hot instruction.
- Counter multiplexing when more events are requested than PMU slots — inflates error.
- Missing or broken unwinding without frame pointers; `[unknown]` frames.
- Inlining collapsing attribution into the caller.
- Overhead concentrating in a region and distorting *that region's* share.
- Serialization the tool itself imposes (e.g. sync-on-every-launch), changing the thing measured.
- Metrics that are per-device vs. per-run vs. per-invocation, silently.

## Permissions and environment

TODO — the exact checks, the exact failure messages, and the fix or the hand-back for each.

## Known-good smoke test

TODO — a tiny program with a known bottleneck, plus the output this tool should produce on it.
Run it when results look implausible; it separates "the code is weird" from "the tool is
misconfigured".
