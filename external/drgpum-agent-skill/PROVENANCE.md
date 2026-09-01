# Provenance

Vendored into ProfileWeaver as `external/drgpum-agent-skill/`, pruned to the core skill.

| | |
|---|---|
| Upstream | https://github.com/jonathan222jr-ai/ml-pgo-skill |
| Branch | main |
| Commit | 2faa25c94228608c469d529082cd48b16c647cc3 |
| Vendored | 2026-08-25 |
| Profiler | [DrGPUM](https://github.com/Lin-Mao/DrGPUM) (Lin, Zhou, Su — ASPLOS '23) |

Local git history was dropped to keep this directory small; ProfileWeaver tracks these
files directly. To recover history or pull updates, re-clone upstream at the commit above
and diff against this tree.

## Removed from the upstream tree

| Path | Size | Why |
|---|---|---|
| `profiler-enhanced/` | 77M | vendored DrGPUM source. Use a real DrGPUM install instead |
| `.git/` | 32M | see above |
| `tests/` | 680K | unit tests + fixtures for `agent/` |
| `findings/` | 260K | captured DrGPUM profiles from the PyTorch allocator case study |
| `benchmarks/` | 52K | benchmark suite runner and results |
| `ML-PGO_Presentation.pptx` | 812K | slides |

## Local modifications

- `.claude/skills/drgpum-memory-pgo/scripts/{analyze_profile,compare_profiles,show_ledger}.py`
  — `sys.path` offset `parents[3]` → `parents[4]`. Upstream moved `skills/` under
  `.claude/` without updating the offset, so every script failed with
  `ModuleNotFoundError: No module named 'agent'`.
