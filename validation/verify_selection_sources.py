"""Bind pre-download annotation decisions to the authentic downloaded SAFE."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.provenance import safe_file_inventory
from cryolens.preprocess.safe_reader import _open_path


def verify_annotation_binding(decision: dict[str, Any], receipt: dict[str, Any]) -> None:
    row = decision["candidate"]
    if receipt["catalogue"]["Id"] != row["id"] or receipt["catalogue"]["Name"] != row["name"]:
        raise ValueError("Selected and downloaded product identity differs")
    for pol in ("HH", "HV"):
        names = [
            node["Name"]
            for node in decision["node_listing"]["result"]
            if f"-{pol.lower()}-" in node["Name"] and node["Name"].endswith(".xml")
        ]
        if (
            len(names) != 1
            or receipt["safe_files_sha256"].get("annotation/" + names[0])
            != decision["channels"][pol]["sha256"]
        ):
            raise ValueError("Pre-download annotation differs from downloaded source: " + pol)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, action="append", required=True)
    p.add_argument("--raw-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve selection-source verification")
    records = []
    for source in args.plan:
        plan = json.loads(source.read_text())
        for row in plan["selected"]:
            decision = next(
                d for d in plan["metadata_decisions"] if d["candidate"]["id"] == row["id"]
            )
            folder = args.raw_root / row["name"].removesuffix(".SAFE")
            receipt_path = folder / "download-receipt.json"
            receipt = json.loads(receipt_path.read_text())
            if not receipt["zip_crc_passed"] or not receipt["manifest_checks"]:
                raise ValueError("Downloaded source lacks archive integrity proof")
            verify_annotation_binding(decision, receipt)
            safe = Path(_open_path(folder / row["name"]))
            if safe_file_inventory(safe) != receipt["safe_files_sha256"]:
                raise ValueError("Downloaded SAFE source inventory changed")
            records.append(
                {
                    "product": row["name"],
                    "plan_sha256": file_digest(source),
                    "download_receipt_sha256": file_digest(receipt_path),
                    "pre_download_annotations_match": True,
                    "complete_source_inventory_matches": True,
                }
            )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "schema_version": 1,
                "driver_sha256": file_digest(Path(__file__)),
                "products": records,
                "claim_boundary": "Source identity and immutability only; no image processing or scientific acceptance.",
            },
            stream,
            indent=2,
        )
    print(
        "Pre-download metadata matches all authentic source inventories:", len(records), flush=True
    )


if __name__ == "__main__":
    main()
