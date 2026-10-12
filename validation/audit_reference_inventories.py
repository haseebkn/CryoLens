"""Audit live development reference bytes without amending historical receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryolens.preprocess.safe_reader import _open_path
from cryolens.preprocess.validation import (
    reference_input_files,
    reference_outputs,
    require_development_product,
    verify_reference_receipt,
)


def hash_bytes(path: Path) -> str:
    """Independently stream the live bytes, without cached digest values."""
    checksum = hashlib.sha256()
    with open(_open_path(path), "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--short-block-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("Audit output must stay in the current worktree")
    if args.output.exists():
        raise FileExistsError("Audit output must be new")
    published = Path("docs/processing-validation/v2")
    config = json.loads(Path("configs/sentinel1-validation-v2.json").read_text())
    manifest = Path(config["frozen_manifest"])
    image = "cryolens-snap-reference:14.0.0"
    runtime = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    audit: dict[str, Any] = {
        "observed_at": datetime.now(UTC).isoformat(),
        "activity": "Post-hoc live reference inventory audit; no SNAP execution or new science",
        "audit_source_sha256": hash_bytes(Path(__file__)),
        "validation_source_sha256": hash_bytes(Path("src/cryolens/preprocess/validation.py")),
        "reference_image_id": runtime,
        "deployment_gate_open": False,
        "products": [],
        "references": [],
    }
    graph_hash = hash_bytes(Path(config["graph"]))
    for product in config["products"]:
        scratch = args.scratch_root / Path(product["path"]).name
        require_development_product(scratch, manifest)
        print("Hashing live consumed inputs: " + scratch.name, flush=True)
        inputs = {
            path.relative_to(scratch).as_posix(): hash_bytes(path)
            for path in reference_input_files(scratch)
        }
        audit["products"].append({"product": scratch.name, "live_inputs_sha256": inputs})
        for row, column in config["sampling_fractions"]:
            name = f"{product['name']}-r{row}-c{column}"
            prefix = args.reference_root / name
            receipt_path = Path(str(prefix) + "-receipt.json")
            receipt_hash = hash_bytes(receipt_path)
            if receipt_hash != hash_bytes(published / "reference-receipts" / receipt_path.name):
                raise ValueError("Published and executed reference receipts differ: " + name)
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["scratch_files_sha256"] != inputs:
                raise ValueError(
                    "Reference consumed-input inventory incomplete or changed: " + name
                )
            if receipt["graph_sha256"] != graph_hash or receipt["image_id"] != runtime:
                raise ValueError("Reference recipe or runtime changed: " + name)
            outputs = {
                path.relative_to(prefix.parent).as_posix(): hash_bytes(path)
                for path in reference_outputs(prefix)
            }
            if receipt["outputs_sha256"] != outputs:
                raise ValueError("Reference output inventory incomplete or changed: " + name)
            audit["references"].append(
                {
                    "name": name,
                    "receipt_sha256": receipt_hash,
                    "consumed_input_files": len(inputs),
                    "output_files": len(outputs),
                    "inventory_complete_and_live_hashes_match": True,
                }
            )
            print("Verified unchanged reference inventory: " + name, flush=True)
    if len(audit["references"]) != 27:
        raise ValueError("Expected the complete 27-window reference plan")
    short_receipt = published / "reference-receipts/short-block-receipt.json"
    short_report = json.loads((published / "short-block-report.json").read_text())
    scratch = args.short_block_root / "scratch" / short_report["product"]
    require_development_product(scratch, manifest)
    prefix = args.short_block_root / "reference/short-block"
    if hash_bytes(Path(str(prefix) + "-receipt.json")) != hash_bytes(short_receipt):
        raise ValueError("Published short-block receipt differs from executed receipt")
    row, column, height, width = short_report["window"]
    try:
        verify_reference_receipt(
            scratch,
            Path("configs/snap/validation-reference.xml"),
            prefix,
            (row, column, height, width),
            image,
        )
    except ValueError as error:
        if "input receipt incomplete" not in str(error):
            raise
        rejection = str(error)
    else:
        raise ValueError("Expected the historical short-block receipt to reject")
    audit["short_block_invalidation"] = {
        "status": "invalidated",
        "reason": rejection,
        "scientific_comparison_accepted": False,
        "historical_artifacts_sha256": {
            name: hash_bytes(published / name)
            for name in (
                "reference-receipts/short-block-receipt.json",
                "short-block-report.json",
                "tracking_receipt.json",
            )
        },
        "historical_mlflow_run_id": json.loads((published / "tracking_receipt.json").read_text())[
            "run_id"
        ],
        "claim_boundary": "The historical short-block signed comparison and its MLflow artifact cannot establish reference acceptance. Full-product radiometric rejection remains. No hashes were added to old caches; replacement acceptance requires a fresh SNAP execution and newly bound report/tracking evidence.",
    }
    audit["completed_at"] = datetime.now(UTC).isoformat()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(audit, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print("27 inventories verified; historical short-block comparison explicitly invalidated")


if __name__ == "__main__":
    main()
