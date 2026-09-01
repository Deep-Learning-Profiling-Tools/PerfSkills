# Provenance

Vendored into ProfileWeaver as `external/hpctoolkit-agent-skill/`, wrapped by the `pw-hpctoolkit`
child skill.

| | |
|---|---|
| Origin | supplied as a loose directory — no git history, no upstream remote |
| Vendored | 2026-08-25 |
| Tool | [HPCToolkit](https://gitlab.com/hpctoolkit/hpctoolkit) — `hpcrun` / `hpcstruct` / `hpcprof` |

Because there is no upstream to pull from, ProfileWeaver tracks these files directly. Edits
here are edits to ProfileWeaver.

## Contents

| File | Role |
|---|---|
| `SKILL.md` | the profiling workflow — verify tools, pick events, run the three stages, report |
| `read_profile.py` | reads an HPCToolkit database with Hatchet and prints GPU-bearing rows |
| `matmul.cu`, `Makefile` | a small CUDA matrix-multiply used as the worked example |

## Repairs applied on merge

The directory arrived with formatting damage consistent with text extracted from a PDF
(`HPCToolkit Codex Skills.pdf`, since removed). Content was preserved; only structure was
restored.

- **`SKILL.md` — newlines stripped throughout.** The YAML frontmatter was collapsed onto a
  single line (`--- name: … description: … ---`), which does not parse as frontmatter at all, so
  the skill could not load. Multi-command code blocks were flattened onto one line — for example
  ` ```bash command -v hpcrun command -v hpcstruct command -v hpcprof ``` ` reads as one command
  with arguments rather than three separate checks. Reconstructed: the `\` line continuations and
  list markers made the original breaks recoverable.
- **`Makefile` — recipe lines indented with spaces instead of tabs**, so `make` failed with
  *missing separator*. Also carried three stray `~` lines (vim empty-buffer markers saved into
  the file). Both fixed; `make -n all` and `make -n clean` now emit the right commands.
- **`read_profile.py` — database path hardcoded** to `hpctoolkit-matmul-database`. Now takes an
  optional argument, defaulting to the same value.

## Not verified

`read_profile.py` needs `hatchet`, which is not installed here, and no HPCToolkit was on `PATH`
at merge time — so the pipeline has not been run end to end in this environment. The repairs
above were verified individually (frontmatter parses, `make -n` is correct, the script's syntax
is valid); the profiling workflow itself has not been exercised.
