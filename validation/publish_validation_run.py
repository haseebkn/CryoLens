"""Publish a completed source-bound processing run; keep rasters in local storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.quality import QualityPolicy
from cryolens.preprocess.validation import verify_reference_receipt


def verify_execution_sources(report: dict[str, Any], snapshot: Path) -> None:
    """Reject a source archive that cannot reproduce the reported processor bytes."""
    required = {
        "validation.py",
        "quality.py",
        "safe_reader.py",
        "python_chain.py",
        "marine_geocoding.py",
        "stack.py",
    }
    if set(report.get("validation_source_sha256", {})) != required:
        raise ValueError("Processing report lacks complete execution source hashes")
    with zipfile.ZipFile(snapshot / "executed-code.zip") as archive:
        recorded = json.loads((snapshot / "source-sha256.json").read_text())
        for member, expected in recorded.items():
            if hashlib.sha256(archive.read(member)).hexdigest() != expected:
                raise ValueError("Source snapshot inventory mismatch")
        for name, expected in report["validation_source_sha256"].items():
            member = "src/cryolens/preprocess/" + name
            if recorded.get(member) != expected:
                raise ValueError("Source snapshot differs from actual execution")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve published evidence; choose a new output")
    report = json.loads((args.input_dir / "report.json").read_text())
    config = json.loads(args.config.read_text())
    if report["configuration_sha256"] != file_digest(args.config):
        raise ValueError("Processing configuration changed")
    if report["graph_sha256"] != file_digest(Path(config["graph"])):
        raise ValueError("Processing graph changed")
    verify_execution_sources(report, args.snapshot_dir)
    pairs = list(zip(config["products"], report["products"], strict=True))
    receipts = []
    for item, product in pairs:
        if Path(item["path"]).name != product["product"]:
            raise ValueError("Processing product inventory differs from plan")
        quality = product["quality"]
        if (
            quality.get("scope") != "full_measurement_grid"
            or quality["policy"] != QualityPolicy().model_dump()
        ):
            raise ValueError("Full quality scan missing or strict policy changed")
        windows = product["windows"]
        if len(windows) != len(config["sampling_fractions"]):
            raise ValueError("Processing window inventory incomplete")
        for fractions, window in zip(config["sampling_fractions"], windows, strict=True):
            prefix = (
                args.input_dir / "reference" / f"{item['name']}-r{fractions[0]}-c{fractions[1]}"
            )
            verify_reference_receipt(
                args.scratch_dir / product["product"],
                Path(config["graph"]),
                prefix,
                tuple(window["window"]),
                report["reference_image"],
            )
            receipt = Path(str(prefix) + "-receipt.json")
            if file_digest(receipt) != window.get("reference_receipt_sha256"):
                raise ValueError("Comparison references a missing or changed receipt")
            receipts.append(receipt)
    args.output.mkdir(parents=True)
    shutil.copyfile(args.input_dir / "report.json", args.output / "report.json")
    shutil.copyfile(args.config, args.output / "configuration.json")
    shutil.copyfile(args.snapshot_dir / "executed-code.zip", args.output / "executed-code.zip")
    shutil.copyfile(args.snapshot_dir / "source-sha256.json", args.output / "source-sha256.json")
    (args.output / "reference-receipts").mkdir()
    for receipt in receipts:
        shutil.copyfile(receipt, args.output / "reference-receipts" / receipt.name)
    for source in args.input_dir.glob("*full-quality.json"):
        shutil.copyfile(source, args.output / source.name)
    (args.output / "logs").mkdir()
    for source in (args.input_dir / "reference").glob("*.log"):
        shutil.copyfile(source, args.output / "logs" / source.name)
    diagnostics = {
        path.relative_to(args.input_dir).as_posix(): file_digest(path)
        for path in sorted((args.input_dir / "diagnostics").rglob("*"))
        if path.is_file()
    }
    (args.output / "local-diagnostics-sha256.json").write_text(json.dumps(diagnostics, indent=2))
    print("Published completed processing execution; gate:", report["deployment_gate_open"])


if __name__ == "__main__":
    main()
