"""Determine whether a prior view can inform scheduling after a source edit.

An unchanged mapped file is a weak historical prior. It cannot certify current
kernel time, counter values, transitive dataflow, or correctness.
"""

from pathlib import Path
from experiment_support import REPO, load
from rewrite_evidence import lineage, identity


def historical(run, state, cid, bound):
    chain = list(lineage(state, cid))
    changes = set()
    for child, origin in zip(chain, chain[1:]):
        patch = run / "candidates" / child / "change.patch"
        if not patch.exists():
            break
        content = patch.read_text()
        files = {s.split("/", 1)[1] for s in content.splitlines()
                 if s.startswith(("+++ candidate/", "--- parent/"))}
        # Non-source changes, build flags, headers and includes can affect the entire program.
        if not files or any(Path(f).suffix not in {".cu", ".c", ".cpp", ".cc", ".cxx", ".f90"}
                            for f in files) or any(s.startswith(("+", "-")) and "#include" in s for s in content.splitlines()):
            break
        changes.update(files)
        facts_dir = run / "facts"
        refs = load(run / "candidates" / origin / "issued-facts.json", {})
        if not refs:
            continue
        root = run / "baseline-source" if origin == "baseline" else run / "candidates" / origin / "source"
        targets = load((run if origin == "baseline" else run / "candidates" / origin / "profile") /
                       "source-targets.json", {})
        if targets.get("unresolved") or not targets.get("targets"):
            continue
        mapped = set()
        for item in targets["targets"]:
            source = (item.get("source") or {}).get("path")
            if not source:
                break
            try:
                mapped.add(str((REPO / source).resolve().relative_to(root.resolve())))
            except ValueError:
                break
        else:
            if changes.isdisjoint(mapped):
                for key in reversed(list(refs)):
                    fact = load(facts_dir / (key + ".json"), {})
                    if (fact.get("binding") == bound and fact.get("fingerprint") == identity(state, origin)
                            and any(item.get("hotspot") == fact.get("target") for item in targets["targets"])):
                        return {"origin": origin, "fact": key, "metric": fact["metric"],
                                "provider": fact["provider"], "role": "historical-mapped-files-untouched"}
    return None
