#!/usr/bin/env python3
"""Build/run/time/correctness-check a GPA-Benchmark case, before and after an
optimization. Two subcommands:

  pgo_bench.py baseline <benchmark>   capture pre-edit reference state
  pgo_bench.py compare  <benchmark>   re-measure after an edit, diff vs baseline,
                                      append a facts-only entry to the ledger
"""

import argparse
import glob
import json
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

SKILL_DIR = SCRIPT_DIR.parent
from _gpa_root import resolve_gpa_root, resolve_toolchain_root, require_benchmark_dir  # noqa: E402
GPA_ROOT = resolve_gpa_root(SKILL_DIR)  # $PW_GPA_ROOT, else legacy parents[2]
BENCHMARKS_CONFIG = SKILL_DIR / "assets" / "benchmarks.json"
LEDGER_TEMPLATE = SKILL_DIR / "assets" / "ledger-template.md"

QDSTRM_IMPORTER_CANDIDATES = [
    "/usr/lib/nsight-systems/host-linux-x64/QdstrmImporter",
    "/opt/nvidia/nsight-systems/*/host-linux-x64/QdstrmImporter",
]

DEFAULT_RUN_TIMEOUT_SECONDS = 60  # generous vs. most benchmarks' ~1-5s runtime; a bad edit
                                  # (e.g. a broken loop-termination condition) can hang the
                                  # binary forever, so every run must be bounded. Slower
                                  # benchmarks (e.g. cfd, ~35s/run) should set their own
                                  # "runTimeoutSeconds" in benchmarks.json -- otherwise a
                                  # legitimately-slower-but-finite run after an edit could
                                  # get misclassified as a hang.

SIGNIFICANCE_MULTIPLE = 2.0  # a before/after delta only counts as real movement if it clears
                             # this many pooled-stddev units (sqrt(sd_before^2 + sd_after^2)).
                             # Not a rigorous t-test (n=3 by default is too small for one), but
                             # it catches the failure mode that motivated this: a single-run
                             # point comparison reporting e.g. "1.042x" with no way to tell
                             # whether that's real movement or run-to-run noise. Added
                             # 2026-08-08 after srad's 4.2% self-reported KEPT verdict (2026-07-19)
                             # was flagged as close enough to noise to need a repeat-and-check.


class BenchmarkTimeout(Exception):
    pass


def load_benchmark(name: str) -> dict:
    config = json.loads(BENCHMARKS_CONFIG.read_text())
    if name not in config:
        sys.exit(f"Unknown benchmark '{name}'. Known: {', '.join(config)}")
    cfg = dict(config[name])
    cfg["path"] = GPA_ROOT / cfg["dir"]
    require_benchmark_dir(cfg["path"], name, GPA_ROOT)
    return cfg


def build(cfg: dict) -> None:
    result = subprocess.run(cfg["buildCmd"], cwd=cfg["path"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Build failed for {cfg['dir']}:\n{result.stdout}\n{result.stderr}")


def timed_runs(cfg: dict) -> tuple[list[float], list[str]]:
    """Run the benchmark N times, return (per-run wall-clock seconds, per-run stdout+stderr).

    Returns raw per-run lists rather than pre-collapsing to a median so callers can compute
    stddev across runs too -- previously only the *last* run's stdout was kept, which meant
    self-reported kernel timing (parsed from stdout) was a single-sample point measurement
    even though end-to-end timing was already medianed across `runs`.
    """
    timeout = cfg.get("runTimeoutSeconds", DEFAULT_RUN_TIMEOUT_SECONDS)
    times = []
    stdouts = []
    for _ in range(cfg.get("runs", 3)):
        start = time.perf_counter()
        try:
            result = subprocess.run(
                cfg["runCmd"], cwd=cfg["path"], capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise BenchmarkTimeout(
                f"run did not finish within {timeout}s "
                f"(likely an infinite loop or broken termination condition introduced by the edit)"
            )
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        stdouts.append(result.stdout + result.stderr)
    return times, stdouts


def _median_stddev(values: list[float]) -> tuple[float, float, int]:
    """(median, stddev, n). stddev is 0.0 when n<2 -- not "no noise", just "not measured"."""
    return statistics.median(values), (statistics.stdev(values) if len(values) > 1 else 0.0), len(values)


def is_significant(base_val: float, base_sd: float, cur_val: float, cur_sd: float) -> bool | None:
    """Whether a before/after delta clears SIGNIFICANCE_MULTIPLE pooled-stddev units.

    Returns None (not True/False) when both stddevs are 0 -- that means fewer than 2 samples
    were available on at least one side, so there's no noise estimate to test against at all;
    treating that as "significant" would silently readmit the single-point-comparison problem
    this exists to catch, and treating it as "not significant" would wrongly suppress a real
    effect just because n was too small to measure variance.
    """
    pooled_sd = (base_sd ** 2 + cur_sd ** 2) ** 0.5
    if pooled_sd == 0.0:
        return None
    return abs(cur_val - base_val) > SIGNIFICANCE_MULTIPLE * pooled_sd


def parse_local_timing(cfg: dict, stdout: str) -> dict:
    """Extract benchmark-self-reported kernel timing from its own stdout, per
    benchmarks.json's "localTiming" list (each entry: label, regex with one
    capture group, unit "ms"|"s", optional 1-based "occurrence" for a regex
    that repeats -- e.g. b+tree's two kernel wrappers print an identical
    "GPU: KERNEL" label, disambiguated only by which one printed first).

    This exists because nsys's CUPTI Activity API kernel-trace capture is a
    confirmed, still-unresolved gap under WSL2 (NVIDIA staff acknowledged this
    on the developer forums; it did not ship in several planned releases and
    is still reported as broken as of early 2026) -- profile_kernels() above
    remains best-effort and is still attempted, but for benchmarks whose own
    source already wraps a kernel launch with std::chrono/cudaEvent/gettimeofday
    and prints the result, that self-reported number is real device-relevant
    timing that doesn't depend on nsys at all. Returns {} if the benchmark has
    no "localTiming" entries or a pattern doesn't match -- never invents a
    number, and never raises on a missing/unmatched pattern.
    """
    results = {}
    for entry in cfg.get("localTiming", []):
        occurrence = entry.get("occurrence", 1)
        matches = list(re.finditer(entry["regex"], stdout))
        if len(matches) < occurrence:
            continue
        value = float(matches[occurrence - 1].group(1))
        unit = entry["unit"]
        if unit == "ms":
            ns = value * 1e6
        elif unit == "s":
            ns = value * 1e9
        else:
            continue  # unrecognized unit -- skip rather than guess a conversion
        results[entry["label"]] = ns
    return results


def find_qdstrm_importer() -> str | None:
    path = shutil.which("QdstrmImporter")
    if path:
        return path
    for candidate in QDSTRM_IMPORTER_CANDIDATES:
        matches = glob.glob(candidate)
        if matches:
            return matches[0]
    return None


def profile_kernels(cfg: dict) -> tuple[dict, str]:
    """Best-effort per-kernel GPU time via nsys -> sqlite export -> direct SQL query.

    Returns (kernel_times_ns, note). kernel_times_ns is {} when kernel-level
    activity genuinely isn't available on this platform (a known limitation on
    some WSL2/driver combinations) -- callers must not treat that as an error,
    just report end-to-end timing instead and surface `note` to the user.
    """
    report_base = cfg["path"] / "pgo-bench-report"
    qdstrm_path = report_base.with_suffix(".qdstrm")
    nsysrep_path = report_base.with_suffix(".nsys-rep")
    sqlite_path = report_base.with_suffix(".sqlite")
    for p in (qdstrm_path, nsysrep_path, sqlite_path):
        p.unlink(missing_ok=True)

    # errors="replace": nsys's own stdout/stderr has been observed to contain
    # non-UTF8 bytes on this platform (e.g. progress-bar control sequences) --
    # without this, a decode failure here raises UnicodeDecodeError, which
    # isn't caught below and crashes the whole compare, defeating the entire
    # point of nsys being best-effort. Never let nsys's own instability be
    # more fatal than "no kernel-level data available."
    profile_cmd = ["nsys", "profile", "--trace=cuda", "-f", "true", "-o", str(report_base)] + cfg["runCmd"]
    try:
        subprocess.run(profile_cmd, cwd=cfg["path"], capture_output=True, text=True, errors="replace", timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError) as e:
        return {}, f"nsys profile failed to run: {e}"

    if not nsysrep_path.exists() and qdstrm_path.exists():
        importer = find_qdstrm_importer()
        if not importer:
            return {}, "nsys produced a .qdstrm but no QdstrmImporter binary was found to convert it"
        subprocess.run([importer, f"--input-file={qdstrm_path}"], capture_output=True, text=True, errors="replace", timeout=180)

    if not nsysrep_path.exists():
        return {}, "nsys produced no usable report file"

    subprocess.run(
        ["nsys", "export", "--type", "sqlite", "--force-overwrite=true",
         "--output", str(sqlite_path), str(nsysrep_path)],
        capture_output=True, text=True, errors="replace", timeout=180,
    )
    if not sqlite_path.exists():
        return {}, "nsys export did not produce a sqlite file"

    kernel_times: dict = {}
    note = "ok"
    try:
        con = sqlite3.connect(sqlite_path)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='CUPTI_ACTIVITY_KIND_KERNEL'")
        if not cur.fetchone():
            note = (
                "nsys captured no GPU kernel activity records on this platform "
                "(known limitation on some WSL2/driver combinations) -- "
                "end-to-end timing is still valid, local kernel timing is not"
            )
        else:
            cur.execute(
                """
                SELECT s.value, SUM(k."end" - k.start)
                FROM CUPTI_ACTIVITY_KIND_KERNEL k
                JOIN StringIds s ON k.shortName = s.id
                GROUP BY s.value
                """
            )
            for name, total_ns in cur.fetchall():
                for target in cfg["kernels"]:
                    if target in name:
                        kernel_times[target] = kernel_times.get(target, 0.0) + total_ns
            if not kernel_times:
                note = "nsys captured kernel activity but none matched the configured kernel name(s)"
        con.close()
    finally:
        for p in (qdstrm_path, nsysrep_path, sqlite_path):
            p.unlink(missing_ok=True)

    return kernel_times, note


def check_correctness(cfg: dict, gpa_db_dir: Path, is_baseline: bool, run_stdout: str) -> tuple[str, str]:
    mode = cfg["correctness"]["mode"]

    if mode == "stdout-marker":
        pass_pat = cfg["correctness"]["passPattern"]
        fail_pat = cfg["correctness"]["failPattern"]
        if pass_pat in run_stdout:
            return "PASS", "stdout contained pass marker"
        if fail_pat in run_stdout:
            return "FAIL", "stdout contained fail marker"
        return "UNKNOWN", "neither pass nor fail marker found in stdout"

    if mode == "output-diff":
        # accepts either a single "outputFile" (existing configs) or a list
        # under "outputFiles" (for benchmarks that write more than one
        # correctness-relevant file, e.g. cfd's density/momentum/density_energy)
        names = cfg["correctness"].get("outputFiles")
        if names is None:
            names = [cfg["correctness"]["outputFile"]]

        total_diffs = 0
        details = []
        for name in names:
            output_file = cfg["path"] / name
            golden_file = gpa_db_dir / f"pgo-baseline-output-{name}"
            if not output_file.exists():
                return "UNKNOWN", f"expected output file missing: {output_file}"
            if is_baseline:
                gpa_db_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy(output_file, golden_file)
                continue
            if not golden_file.exists():
                return "UNKNOWN", "no golden reference captured yet — run 'baseline' first"
            current_lines = output_file.read_text().splitlines()
            golden_lines = golden_file.read_text().splitlines()
            if current_lines != golden_lines:
                diffs = sum(1 for a, b in zip(current_lines, golden_lines) if a != b)
                diffs += abs(len(current_lines) - len(golden_lines))
                total_diffs += diffs
                details.append(f"{name}: {diffs} differing/missing line(s)")

        if is_baseline:
            return "PASS", "baseline output captured as golden reference"
        if total_diffs == 0:
            return "PASS", "output matches golden reference exactly" if len(names) == 1 else f"all {len(names)} output files match golden reference exactly"
        return "FAIL", "; ".join(details) + " vs golden reference"

    if mode == "stdout-diff":
        # golden reference is stored as JSON (not newline-joined text) because
        # "\n".join(lines) does not round-trip symmetrically through .splitlines()
        # when `lines` has trailing empty strings -- that mismatch previously
        # produced a false-positive FAIL on output that was actually identical.
        ignore_patterns = cfg["correctness"].get("ignorePatterns", [])
        golden_file = gpa_db_dir / "pgo-baseline-stdout.json"
        current_lines = [
            line for line in run_stdout.splitlines()
            if not any(pat in line for pat in ignore_patterns)
        ]
        if is_baseline:
            gpa_db_dir.mkdir(parents=True, exist_ok=True)
            golden_file.write_text(json.dumps(current_lines))
            return "PASS", "baseline stdout captured as golden reference"
        if not golden_file.exists():
            return "UNKNOWN", "no golden reference captured yet — run 'baseline' first"
        golden_lines = json.loads(golden_file.read_text())
        if current_lines == golden_lines:
            return "PASS", "stdout (minus ignored/non-deterministic lines) matches golden reference exactly"
        diffs = sum(1 for a, b in zip(current_lines, golden_lines) if a != b)
        diffs += abs(len(current_lines) - len(golden_lines))
        return "FAIL", f"{diffs} differing/missing line(s) vs golden stdout reference"

    return "UNKNOWN", f"unrecognized correctness mode: {mode}"


def problem_size_fingerprint(cfg: dict) -> str:
    """Deterministic problem-size label derived from runCmd's own arguments (excludes the
    binary path itself, e.g. bfs's runCmd ["./bfs", "../../data/bfs/graph1MW_6.txt"] -> the
    dataset path). Using the actual invocation args -- not free-text prose an agent might
    describe inconsistently or forget -- keeps ledger entries machine-comparable across sizes:
    a REVERTED verdict recorded at one problemSize should not be read as covering every size.
    """
    return " ".join(str(a) for a in cfg["runCmd"][1:]) or "(no args)"


def measure(benchmark: str, is_baseline: bool) -> dict:
    cfg = load_benchmark(benchmark)
    gpa_db_dir = cfg["path"] / "gpa-database"

    build(cfg)
    try:
        end_to_end_times, stdouts = timed_runs(cfg)
    except BenchmarkTimeout as e:
        return {
            "benchmark": benchmark,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "problemSize": problem_size_fingerprint(cfg),
            "endToEndSeconds": None,
            "endToEndStddevSeconds": None,
            "endToEndRuns": 0,
            "kernelTimesNs": {},
            "localTimingNote": "not profiled — run timed out",
            "selfReportedKernelTimesNs": {},
            "selfReportedKernelStddevNs": {},
            "selfReportedKernelRunsN": {},
            "correctness": "FAIL",
            "correctnessDetail": str(e),
        }
    last_stdout = stdouts[-1]
    end_to_end_seconds, end_to_end_stddev_seconds, end_to_end_runs = _median_stddev(end_to_end_times)

    kernel_times_ns, local_timing_note = profile_kernels(cfg)

    # Parse self-reported kernel timing from EVERY run's stdout, not just the last one --
    # each label's values are collected across whichever runs actually printed it (a label
    # can legitimately be absent from some runs, e.g. a conditional code path), then reduced
    # to median/stddev/n exactly like end-to-end timing already was.
    per_run_self_reported = [parse_local_timing(cfg, s) for s in stdouts]
    self_reported_kernel_times_ns: dict = {}
    self_reported_kernel_stddev_ns: dict = {}
    self_reported_kernel_runs_n: dict = {}
    all_labels = {label for run in per_run_self_reported for label in run}
    for label in all_labels:
        values = [run[label] for run in per_run_self_reported if label in run]
        median_ns, stddev_ns, n = _median_stddev(values)
        self_reported_kernel_times_ns[label] = median_ns
        self_reported_kernel_stddev_ns[label] = stddev_ns
        self_reported_kernel_runs_n[label] = n

    correctness, correctness_detail = check_correctness(cfg, gpa_db_dir, is_baseline, last_stdout)

    return {
        "benchmark": benchmark,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "problemSize": problem_size_fingerprint(cfg),
        "endToEndSeconds": end_to_end_seconds,
        "endToEndStddevSeconds": end_to_end_stddev_seconds,
        "endToEndRuns": end_to_end_runs,
        "kernelTimesNs": kernel_times_ns,
        "localTimingNote": local_timing_note,
        "selfReportedKernelTimesNs": self_reported_kernel_times_ns,
        "selfReportedKernelStddevNs": self_reported_kernel_stddev_ns,
        "selfReportedKernelRunsN": self_reported_kernel_runs_n,
        "correctness": correctness,
        "correctnessDetail": correctness_detail,
    }


def cmd_baseline(args):
    result = measure(args.benchmark, is_baseline=True)
    cfg = load_benchmark(args.benchmark)
    gpa_db_dir = cfg["path"] / "gpa-database"
    gpa_db_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = gpa_db_dir / "pgo-baseline.json"
    baseline_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"\nBaseline written to {baseline_path}", file=sys.stderr)


def cmd_compare(args):
    cfg = load_benchmark(args.benchmark)
    gpa_db_dir = cfg["path"] / "gpa-database"
    baseline_path = gpa_db_dir / "pgo-baseline.json"
    if not baseline_path.exists():
        sys.exit(f"No baseline found at {baseline_path} — run 'baseline' before editing source.")
    baseline = json.loads(baseline_path.read_text())

    current = measure(args.benchmark, is_baseline=False)

    ledger_path = gpa_db_dir / "pgo-ledger.md"
    if not ledger_path.exists():
        template = LEDGER_TEMPLATE.read_text().replace("<BENCHMARK>", args.benchmark)
        ledger_path.write_text(template)

    if current["endToEndSeconds"] is None:
        # timed out before any measurement was possible — nothing to compute speedup from
        result = {**current}
        print(json.dumps(result, indent=2))
        entry_lines = [
            f"\n### {result['timestamp']} — {args.benchmark}",
            f"- Problem size: {result['problemSize']}",
            f"- Correctness: {result['correctness']} ({result['correctnessDetail']})",
        ]
        with ledger_path.open("a") as f:
            f.write("\n".join(entry_lines) + "\n")
        print(f"\nLedger updated at {ledger_path} (facts only — agent must append its KEPT/REVERTED decision)", file=sys.stderr)
        return

    end_to_end_speedup = baseline["endToEndSeconds"] / current["endToEndSeconds"]
    end_to_end_significant = is_significant(
        baseline["endToEndSeconds"], baseline.get("endToEndStddevSeconds") or 0.0,
        current["endToEndSeconds"], current["endToEndStddevSeconds"],
    )
    kernel_speedups = {}
    for name, base_ns in baseline["kernelTimesNs"].items():
        cur_ns = current["kernelTimesNs"].get(name)
        if cur_ns:
            kernel_speedups[name] = base_ns / cur_ns

    # baseline.get(...) not baseline[...]: a baseline captured before this field
    # existed won't have it -- degrade gracefully (empty dict) rather than KeyError.
    self_reported_speedups = {}
    self_reported_significant = {}
    baseline_self_sd = baseline.get("selfReportedKernelStddevNs", {})
    for name, base_ns in baseline.get("selfReportedKernelTimesNs", {}).items():
        cur_ns = current["selfReportedKernelTimesNs"].get(name)
        if cur_ns:
            self_reported_speedups[name] = base_ns / cur_ns
            self_reported_significant[name] = is_significant(
                base_ns, baseline_self_sd.get(name, 0.0),
                cur_ns, current["selfReportedKernelStddevNs"].get(name, 0.0),
            )

    result = {
        **current,
        "baselineEndToEndSeconds": baseline["endToEndSeconds"],
        "endToEndSpeedup": end_to_end_speedup,
        "endToEndSignificant": end_to_end_significant,
        "kernelSpeedups": kernel_speedups,
        "selfReportedKernelSpeedups": self_reported_speedups,
        "selfReportedKernelSignificant": self_reported_significant,
    }
    print(json.dumps(result, indent=2))

    def sig_label(sig: bool | None) -> str:
        if sig is None:
            return "n<2 samples on at least one side, no noise estimate"
        return f"clears {SIGNIFICANCE_MULTIPLE:g}σ" if sig else f"NOT SIGNIFICANT, within {SIGNIFICANCE_MULTIPLE:g}σ noise"

    baseline_problem_size = baseline.get("problemSize", "(unknown — predates problem-size tracking)")
    problem_size_line = f"- Problem size: {current['problemSize']}"
    if baseline_problem_size != current["problemSize"]:
        problem_size_line += (
            f" (baseline was measured at: {baseline_problem_size} — this is a deliberate "
            f"different-size comparison, not a like-for-like retest; do not treat the speedup "
            f"below as validating/invalidating the baseline's original problem size)"
        )

    entry_lines = [
        f"\n### {result['timestamp']} — {args.benchmark}",
        problem_size_line,
        f"- Correctness: {result['correctness']} ({result['correctnessDetail']})",
        f"- End-to-end: {baseline['endToEndSeconds']:.4f}s (σ={baseline.get('endToEndStddevSeconds') or 0.0:.4f}, n={baseline.get('endToEndRuns', '?')}) -> "
        f"{current['endToEndSeconds']:.4f}s (σ={current['endToEndStddevSeconds']:.4f}, n={current['endToEndRuns']}) "
        f"({end_to_end_speedup:.3f}x) [{sig_label(end_to_end_significant)}]",
    ]
    if kernel_speedups:
        for name, speedup in kernel_speedups.items():
            entry_lines.append(
                f"- Kernel {name}: {baseline['kernelTimesNs'][name]:.0f}ns -> {current['kernelTimesNs'][name]:.0f}ns ({speedup:.3f}x)"
            )
    else:
        entry_lines.append(f"- Local kernel timing (nsys): unavailable ({current['localTimingNote']})")
    if self_reported_speedups:
        baseline_self_ns = baseline["selfReportedKernelTimesNs"]
        baseline_self_n = baseline.get("selfReportedKernelRunsN", {})
        for name, speedup in self_reported_speedups.items():
            entry_lines.append(
                f"- Self-reported kernel timing {name}: {baseline_self_ns[name]:.0f}ns "
                f"(σ={baseline_self_sd.get(name, 0.0):.0f}, n={baseline_self_n.get(name, '?')}) -> "
                f"{current['selfReportedKernelTimesNs'][name]:.0f}ns "
                f"(σ={current['selfReportedKernelStddevNs'][name]:.0f}, n={current['selfReportedKernelRunsN'][name]}) "
                f"({speedup:.3f}x) [{sig_label(self_reported_significant[name])}]"
            )
    with ledger_path.open("a") as f:
        f.write("\n".join(entry_lines) + "\n")
    print(f"\nLedger updated at {ledger_path} (facts only — agent must append its KEPT/REVERTED decision)", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_baseline = sub.add_parser("baseline", help="Capture pre-edit reference state")
    p_baseline.add_argument("benchmark")
    p_baseline.set_defaults(func=cmd_baseline)

    p_compare = sub.add_parser("compare", help="Re-measure after an edit and diff vs baseline")
    p_compare.add_argument("benchmark")
    p_compare.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
