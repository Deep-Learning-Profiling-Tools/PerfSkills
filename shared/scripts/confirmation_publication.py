"""Keep a verified result intact when an explicitly requested challenge fails."""

from contextlib import contextmanager
from copy import deepcopy
import shutil


@contextmanager
def protect(run, state):
    prior = deepcopy(state)
    label = "attempt-%02d" % (len(state.get("batches", [])) + len(state.get("confirmation_attempts", [])) + 1)
    archive = run / "confirmation-attempts" / label
    archive.mkdir(parents=True, exist_ok=True)
    files = {p.name: p.read_bytes() for p in run.iterdir()
             if p.is_file() and p.suffix in {".json", ".txt", ".md"}}
    files.update({str(p.relative_to(run)): p.read_bytes() for p in (run / "candidates").glob("*/evidence.json")})
    directories = ("ncu-final", "final-profile", "final-cupti", "raw/final", "raw/cupti/final")
    for name in directories:
        if (run / name).is_dir():
            shutil.copytree(run / name, archive / "previous" / name)
    outcome = {"commit": False}
    try:
        yield outcome
    finally:
        if not outcome["commit"]:
            for p in list(run.iterdir()):
                if p.is_file() and p.suffix in {".json", ".txt", ".md"}:
                    if p.name not in files or p.read_bytes() != files[p.name]:
                        q = archive / "rejected" / p.name
                        q.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, q)
                    if p.name not in files:
                        p.unlink()
            for name, data in files.items():
                current = run / name
                if "/" in name and current.is_file() and current.read_bytes() != data:
                    q = archive / "rejected" / name
                    q.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(current, q)
                (run / name).write_bytes(data)
            for name in directories:
                current, previous = run / name, archive / "previous" / name
                if current.exists():
                    rejected = archive / "rejected" / name
                    rejected.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(current), str(rejected))
                if previous.exists():
                    current.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(previous, current)
            state.clear()
            state.update(prior)
            state.setdefault("confirmation_attempts", []).append({
                "status": "not-published", "artifact": str(archive),
                "published_winner": prior["confirmed"]["id"]})
            from experiment_state import save_state
            save_state(run, state)
