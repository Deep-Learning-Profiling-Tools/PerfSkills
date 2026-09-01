# Coverage-check methodology

How to answer "is this candidate CUDA benchmark already covered by GvProf, DrGPUM, or GPA?" —
used before onboarding a benchmark surfaced by a paper survey, so the survey doesn't claim a gap
that doesn't exist.

## Why this needs a defined methodology at all

None of the three tools publish one canonical "here is everything we've ever tested" list — their
papers' evaluation sections may include benchmarks beyond what's in their public repos, and a
public repo's benchmark suite can grow after a paper is published. **Absence of evidence is not
evidence of absence.** Treat every result as `"found"`, `"not-found"` (best available evidence,
not proof), or `"unknown"` — never assert a hard negative.

## How to check

```
python3 external/gpa-agent-skill/scripts/check_coverage.py <candidate-name>
```

This checks the candidate's name against `assets/known-coverage.json`, a cited snapshot gathered
by directly inspecting each tool's real public repo (not general knowledge or a guess):

- **GvProf** (`GVProf/GVProf`) and **DrGPUM** (`Lin-Mao/DrGPUM`) both point their `samples`
  git submodule at the *same* shared repo, `FindHao/hpctoolkit-gpu-sanitizer-samples` — confirmed
  by reading both `.gitmodules` files directly. For coverage purposes, treat GvProf and DrGPUM as
  one combined suite, not two independent checks — a benchmark can't logically be "covered by
  GvProf but not DrGPUM" under this snapshot.
- **GPA** (`Jokeren/GPA`) uses `Jokeren/GPA-Benchmark` (the rodinia fork this repo is itself forked
  from) — confirmed via GPA's own README example (`./bin/bench.sh rodinia/bfs`) and the repo's
  actual directory contents.

## Known gaps as of the last snapshot (2026-07-17)

- `kmeans` is covered by GPA but absent from the GvProf/DrGPUM shared suite.
- `dwt2d`, `hotspot3D`, and `nn` are in the GvProf/DrGPUM shared suite but absent from
  GPA-Benchmark's rodinia set.
- `leukocyte`, `mummergpu`, and `hybridsort` appear in *neither* list checked here. That only means
  neither GvProf/DrGPUM's nor GPA's public benchmark suites include them — it does not mean no
  other GPU profiling tool anywhere has ever tested them, and it does not by itself make them good
  candidates from "recent conference papers" (item 2's actual ask) — it's a starting point for
  investigation, not a finding.

## When the snapshot is stale

`known-coverage.json`'s `_meta.lastChecked` records when this was last verified against the real
repos. If it's more than a few months old, re-run the same `gh api repos/<owner>/<repo>/contents`
/ `.gitmodules` inspection this snapshot was built from before trusting a `"not-found"` result for
anything load-bearing (i.e., before actually onboarding a "gap" candidate into `benchmarks.json`).

## What this does not do

This methodology checks *benchmark suite membership*, not "has this exact optimization already
been suggested by GPA's advisor for this kernel." A benchmark can be `"found"` (present in a
tool's suite) while still having missed sub-optimizations worth investigating — that's a separate
question, covered by the advisor-miss investigation approach (see the `findRangeK` case study at
`GPA-Benchmark/rodinia/b+tree/gpa-database/findRangeK-advisor-gap.md`), not by this script.
