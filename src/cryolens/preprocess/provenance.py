"""Original SAFE input byte inventories for strict processing-evidence joins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path


def safe_source_inventory(reader: SAFEProductReader) -> dict[str, str]:
    safe = Path(_open_path(reader.safe_dir))
    paths = [
        safe / "manifest.safe",
        *sorted((safe / "measurement").glob("*.tiff")),
        *sorted((safe / "annotation").rglob("*.xml")),
    ]
    if len(paths) < 4:
        raise ValueError("Original SAFE source inventory incomplete")
    return {path.relative_to(safe).as_posix(): file_digest(path) for path in paths}


def original_inventory(provenance: dict[str, Any]) -> dict[str, str]:
    try:
        inventory = {
            "manifest.safe": provenance["original_manifest_sha256"],
            **provenance["annotation_sha256"],
        }
        for item in provenance["measurements"]:
            name = item["measurement"]
            if Path(name).name != name or "\\" in name:
                raise ValueError("Invalid source measurement name")
            inventory["measurement/" + name] = item["source_sha256"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Original processing source inventory missing") from exc
    if len(provenance["measurements"]) < 2 or not provenance["annotation_sha256"]:
        raise ValueError("Original processing source inventory incomplete")
    if any(not isinstance(value, str) or len(value) != 64 for value in inventory.values()):
        raise ValueError("Invalid original source hashes")
    return inventory
