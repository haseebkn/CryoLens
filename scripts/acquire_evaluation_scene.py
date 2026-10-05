"""Download a new publisher-labelled AI4Arctic scene for metadata-only freezing.

Reads no SAR or chart arrays. Requires a new local filename; existing files are
never overwritten. Selection is based on acquisition metadata, not performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import requests
from netCDF4 import Dataset

from cryolens.eval.cohort import acquisition_id, file_digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="20190119T101218_cis_prep.nc")
    parser.add_argument(
        "--output", type=Path, default=Path("data/raw/evaluation_holdout/ai4arctic")
    )
    parser.add_argument(
        "--receipt", type=Path, default=Path("configs/evaluation/new-source-v1.json")
    )
    args = parser.parse_args()
    catalogue_url = "https://api.figshare.com/v2/articles/21316608"
    response = requests.get(catalogue_url, timeout=60)
    response.raise_for_status()
    catalogue = response.json()
    entry = next(f for f in catalogue["files"] if f["name"] == args.name)
    destination = args.output / args.name
    if any(Path("data/raw/ai4arctic").rglob(args.name)):
        raise ValueError(
            "Acquisition already exists in the development archive; a duplicate download is not untouched data"
        )
    if destination.exists() or args.receipt.exists():
        raise FileExistsError("New evaluation inputs and receipts cannot overwrite existing files")
    args.output.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".nc.part")
    md5 = hashlib.md5(usedforsecurity=False)
    with requests.get(entry["download_url"], stream=True, timeout=(30, 120)) as download:
        download.raise_for_status()
        with partial.open("xb") as stream:
            for chunk in download.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
                    md5.update(chunk)
    if partial.stat().st_size != entry["size"] or md5.hexdigest() != entry["supplied_md5"]:
        raise ValueError("Downloaded data do not match publisher size/MD5")
    partial.rename(destination)
    with Dataset(str(destination)) as dataset:
        identifier = acquisition_id(str(dataset.original_id))
        # Presence is checked; chart values are kept uninspected.
        if "SIC" not in dataset.variables:
            raise ValueError("Publisher-labelled input lacks SIC variable")
    receipt = {
        "schema_version": 1,
        "catalogue_url": catalogue_url,
        "publisher_doi": catalogue["doi"],
        "publisher_article_version": catalogue["version"],
        "scene_id": args.name,
        "source_path": destination.relative_to(Path("data/raw")).as_posix(),
        "acquisition_id": identifier,
        "download_url": entry["download_url"],
        "source_bytes": entry["size"],
        "publisher_md5": entry["supplied_md5"],
        "source_sha256": file_digest(destination),
        "exposure": "metadata_integrity_only",
        "reason": "New download selected by acquisition time/name; only file integrity and NetCDF metadata inspected. SAR and chart arrays remain uninspected.",
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
