"""Locate only the bounded source dependencies of acquired evidence."""

from pathlib import Path
import subprocess
import sys
from acquisition_limits import remaining
from experiment_support import REPO, display


def _locate_source(run: Path, workspace: Path, profile: Path, output: Path,
                   raw: Path) -> None:
    argv = [sys.executable, str(REPO / "shared/scripts/locate_source.py"),
            "--profile", str(profile), "--run-dir", str(output.parent),
            "--source-root", str(workspace), "--repo-root", str(REPO),
            "--output", str(output), "--max-targets", "4",
            "--max-variants", "3", "--dependency-depth", "3", "--max-dependencies", "9"]
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=remaining(60))
    (raw / "locate.stdout.txt").write_text(proc.stdout)
    (raw / "locate.stderr.txt").write_text(proc.stderr)
    if proc.returncode:
        raise RuntimeError(f"source dependency location failed; see {display(raw)}")

