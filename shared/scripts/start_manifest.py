"""Load an optional workload contract or infer one from standard build files."""

from __future__ import annotations

from pathlib import Path

from experiment_support import REPO, load
from validate import errors_for
from workload_infer import infer


NAME = "profileweaver.json"


def manifest_path(value: Path) -> Path:
    path = value.resolve()
    return path / NAME if path.is_dir() else path


def load_manifest(value: Path) -> tuple[dict, Path | None, Path]:
    path = manifest_path(value)
    source = value.resolve() if value.resolve().is_dir() else path.parent.resolve()
    manifest = load(path, None)
    if manifest is None and path.exists():
        raise ValueError(f"invalid workload manifest JSON: {path}")
    if manifest is None:
        manifest = infer(source)
        manifest_path_value = None
    else:
        manifest_path_value = path
    schema = load(REPO / "shared/schema/workload.schema.json", {})
    errors, _ = errors_for(schema, manifest)
    if errors:
        raise ValueError("invalid workload manifest: " + "; ".join(errors[:3]))
    if manifest_path_value is None:
        return manifest, None, source
    source = Path(manifest.get("source", "."))
    source = source if source.is_absolute() else path.parent / source
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"workload source directory is missing: {source}")
    return manifest, manifest_path_value, source
