#!/usr/bin/env python3
"""
ML-PGO agent: Claude-assisted GPU memory optimization from DrGPUM profiles.

Three stages, matching the project's skill definition (.claude/skills/drgpum-memory-pgo/SKILL.md):

  1. analyze_profile()      parse a DrGPUM memory_liveness directory, rank
                            inefficiencies, and have Claude diagnose them
  2. suggest_optimizations() ranked, source-level fixes for the top finding
  3. validate_results()      compare before/after profiles and decide whether
                            to iterate or stop

The detection in stage 1 is deterministic and runs locally -- Claude is asked to
interpret findings and locate them in source, never to invent metrics. That
separation is deliberate: every number in a report traces back to the profiler.

Usage:
    python -m agent.ml_pgo_agent --baseline drgpum_out/
    python -m agent.ml_pgo_agent --baseline before/ --optimized after/
    python -m agent.ml_pgo_agent --baseline before/ --source model.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.drgpum import (  # noqa: E402
    DrGPUMParseError,
    analyze,
    format_report,
    human_bytes,
    parse_profile,
)
from agent.drgpum.report import profile_summary as _summary  # noqa: E402

DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """\
You are a GPU memory optimization engineer working with DrGPUM, a memory
profiler for CUDA and PyTorch applications (ASPLOS '23).

What DrGPUM measures: allocation, transfer, access, and free events against
memory objects, plus a peak-vs-optimal memory summary. What it does NOT measure:
kernel runtime, SM occupancy, memory bandwidth. Never cite those -- they do not
exist in this data. If a question requires them, say so and stop.

Idle spans are counted in kernel launches because the profiler emits no
timestamps. "Idle byte-kernels" means bytes resident times kernel launches
during which those bytes went untouched.

Ground every claim in a specific object id, byte count, and op_id from the
findings you are given. Do not estimate speedups. Only allocations live at the
peak kernel can reduce peak memory -- say so when a finding is not live there.
"""


class MLPGOAgent:
    """Claude-assisted analysis over deterministic DrGPUM findings."""

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL):
        # A bare Anthropic() also resolves ANTHROPIC_API_KEY or an `ant auth
        # login` profile, so don't require an explicit key.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.history: List[Dict[str, Any]] = []

    # -- Claude plumbing ----------------------------------------------------

    def _ask(self, prompt: str, effort: str = "high") -> str:
        messages = self.history + [{"role": "user", "content": prompt}]
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                messages=messages,
            )
        except anthropic.NotFoundError as e:
            raise SystemExit(f"Unknown model {self.model!r}: {e}") from e
        except anthropic.AuthenticationError as e:
            raise SystemExit(
                "No valid Anthropic credentials. Set ANTHROPIC_API_KEY or run "
                "`ant auth login`."
            ) from e
        except anthropic.RateLimitError as e:
            retry = e.response.headers.get("retry-after", "60")
            raise SystemExit(f"Rate limited; retry after {retry}s.") from e
        except anthropic.APIStatusError as e:
            raise SystemExit(f"API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise SystemExit(f"Could not reach the Anthropic API: {e}") from e

        if response.stop_reason == "refusal":
            raise SystemExit("Request was declined by safety classifiers.")

        text = "".join(b.text for b in response.content if b.type == "text")
        self.history.append({"role": "user", "content": prompt})
        self.history.append({"role": "assistant", "content": text})
        return text

    # -- stage 1 ------------------------------------------------------------

    def analyze_profile(self, profile_dir: str, top: int = 8) -> Dict[str, Any]:
        """Parse, rank, and have Claude diagnose a DrGPUM profile."""
        print("\n" + "=" * 70)
        print("STAGE 1: PROFILE ANALYSIS")
        print("=" * 70)

        profile = parse_profile(profile_dir)
        findings = analyze(profile)
        print(format_report(profile, findings, top=top))

        if not findings:
            return {"profile": _summary(profile), "findings": [], "claude_analysis": None}

        payload = {
            "profile": _summary(profile),
            "findings": [f.to_dict() for f in findings[:top]],
        }

        analysis = self._ask(
            "Here are deterministic findings from a DrGPUM memory_liveness "
            "profile. The evidence, harm, and fix fields were generated by the "
            "detectors, not by you.\n\n"
            f"{json.dumps(payload, indent=2)}\n\n"
            "Diagnose this profile:\n"
            "1. What overall pattern do these findings suggest about how the "
            "application manages memory?\n"
            "2. Which findings are likely the same root cause seen from "
            "different angles?\n"
            "3. Which are live at the peak kernel and therefore actually "
            "capable of reducing peak memory?\n"
            "4. What would you need to see in the source to confirm each?\n"
            "Be specific and cite object ids."
        )
        print("\n--- Claude's diagnosis ---\n")
        print(analysis)

        return {
            "profile": _summary(profile),
            "findings": [f.to_dict() for f in findings],
            "claude_analysis": analysis,
        }

    # -- stage 2 ------------------------------------------------------------

    def suggest_optimizations(
        self, analysis: Dict[str, Any], source: Optional[str] = None
    ) -> Dict[str, Any]:
        """Ask for source-level fixes, optionally against real source text."""
        print("\n" + "=" * 70)
        print("STAGE 2: OPTIMIZATION SUGGESTIONS")
        print("=" * 70)

        findings = analysis.get("findings", [])
        if not findings:
            print("No findings to act on.")
            return {"suggestions": None}

        source_block = ""
        if source:
            text = Path(source).read_text(errors="replace")
            source_block = (
                f"\n\nHere is the application source ({source}):\n"
                f"```python\n{text}\n```\n"
                "Map findings onto specific lines. If you cannot identify the "
                "allocation site for a finding, say so rather than guessing."
            )

        suggestions = self._ask(
            "Propose fixes for the top findings, ranked by "
            "(bytes recoverable) / (implementation risk).\n\n"
            f"{json.dumps({'findings': findings[:5]}, indent=2)}"
            f"{source_block}\n\n"
            "For each: the concrete code change, why it is "
            "semantics-preserving, how to verify correctness after applying "
            "it, and the expected effect on current_memory_peak in bytes. "
            "Fix exactly one finding per iteration -- say which one to do "
            "first and why."
        )
        print(suggestions)
        return {"suggestions": suggestions}

    # -- stage 3 ------------------------------------------------------------

    def validate_results(self, baseline_dir: str, optimized_dir: str) -> Dict[str, Any]:
        """Compare two profiles and decide whether to iterate."""
        print("\n" + "=" * 70)
        print("STAGE 3: VALIDATION")
        print("=" * 70)

        before = parse_profile(baseline_dir)
        after = parse_profile(optimized_dir)

        b_peak, a_peak = before.current_memory_peak, after.current_memory_peak
        comparison: Dict[str, Any] = {
            "baseline": _summary(before),
            "optimized": _summary(after),
        }
        if b_peak and a_peak:
            comparison["peak_reduction_bytes"] = b_peak - a_peak
            comparison["peak_reduction_pct"] = (b_peak - a_peak) / b_peak * 100
            print(f"  Peak before : {human_bytes(b_peak)}")
            print(f"  Peak after  : {human_bytes(a_peak)}")
            print(
                f"  Reduction   : {human_bytes(b_peak - a_peak)} "
                f"({comparison['peak_reduction_pct']:.1f}%)"
            )

        before_kinds = {(f.kind, f.object_id) for f in analyze(before)}
        after_kinds = {(f.kind, f.object_id) for f in analyze(after)}
        comparison["resolved"] = sorted(
            f"{k}:{o}" for k, o in before_kinds - after_kinds
        )
        comparison["introduced"] = sorted(
            f"{k}:{o}" for k, o in after_kinds - before_kinds
        )

        assessment = self._ask(
            "Compare these two DrGPUM profiles, captured before and after one "
            "source change.\n\n"
            f"{json.dumps(comparison, indent=2)}\n\n"
            "Assess:\n"
            "1. Is the peak reduction real and attributable to the change?\n"
            "2. Did any new inefficiency appear?\n"
            "3. Do any termination clauses from the skill now apply "
            "(reduction under 2%, within 10% of optimal, no finding above 1% "
            "severity)?\n"
            "4. Iterate or stop -- and if iterate, on which finding?\n"
            "Remember a peak reduction is only valid if correctness was "
            "verified separately; flag if that is unstated."
        )
        print("\n--- Claude's assessment ---\n")
        print(assessment)

        return {"comparison": comparison, "claude_assessment": assessment}

    # -- convenience --------------------------------------------------------

    def run(
        self,
        baseline_dir: str,
        optimized_dir: Optional[str] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        results["analysis"] = self.analyze_profile(baseline_dir)
        results["suggestions"] = self.suggest_optimizations(results["analysis"], source)
        if optimized_dir:
            results["validation"] = self.validate_results(baseline_dir, optimized_dir)
        return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m agent.ml_pgo_agent",
        description="Claude-assisted GPU memory optimization from DrGPUM profiles.",
    )
    p.add_argument("--baseline", required=True, help="DrGPUM output directory (before)")
    p.add_argument("--optimized", help="DrGPUM output directory (after), enables stage 3")
    p.add_argument("--source", help="application source file, for locating findings")
    p.add_argument("--output", default="pgo_results.json", help="where to write results")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    args = p.parse_args(argv)

    try:
        agent = MLPGOAgent(api_key=args.api_key, model=args.model)
        results = agent.run(args.baseline, args.optimized, args.source)
    except DrGPUMParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    Path(args.output).write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
