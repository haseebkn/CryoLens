"""Execute a new real SNAP comparison, preserving invalidated historical evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import rasterio

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path
from cryolens.preprocess.validation import (
    compare_power,
    read_reference,
    reference_identity,
    reference_input_files,
    require_development_product,
    run_reference,
    verify_reference_receipt,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--safe", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish", type=Path, required=True)
    args = parser.parse_args()
    for destination in (args.output, args.publish):
        if not destination.resolve().is_relative_to(Path.cwd().resolve()) or destination.exists():
            raise ValueError("Use new output and publication folders inside the current worktree")
    config_path = Path("configs/sentinel1-short-block-validation.json")
    config = json.loads(config_path.read_text())
    if args.safe.name != config["product"] or args.scratch.name != config["product"]:
        raise ValueError("Short-block inputs must be the declared September 11 development product")
    require_development_product(args.safe, Path(config["frozen_manifest"]))
    baseline_path = Path("docs/processing-validation/v2/short-block-report.json")
    baseline = json.loads(baseline_path.read_text())
    provenance = baseline["source_provenance"]
    actual = {
        path.relative_to(args.safe).as_posix(): file_digest(Path(_open_path(path)))
        for path in reference_input_files(args.safe)
    }
    if actual["manifest.safe"] != provenance["original_manifest_sha256"]:
        raise ValueError("Original development manifest changed")
    for name, expected in provenance["annotation_sha256"].items():
        if actual[name] != expected or file_digest(Path(_open_path(args.scratch / name))) != expected:
            raise ValueError("Original or scratch annotation changed: " + name)
    if file_digest(Path(_open_path(args.scratch / "manifest.safe"))) != provenance["scratch_manifest_sha256"]:
        raise ValueError("Scratch manifest adaptation changed")
    for measurement in provenance["measurements"]:
        name = "measurement/" + measurement["measurement"]
        if actual[name] != measurement["source_sha256"]:
            raise ValueError("Original development measurement changed")
        source_hash, scratch_hash = hashlib.sha256(), hashlib.sha256()
        with (
            rasterio.open(_open_path(args.safe / name)) as source,
            rasterio.open(_open_path(args.scratch / name)) as scratch,
        ):
            if source.shape != scratch.shape or source.dtypes != scratch.dtypes:
                raise ValueError("Scratch measurement shape/type differs")
            for _, window in scratch.block_windows(1):
                source_hash.update(source.read(1, window=window).tobytes())
                scratch_hash.update(scratch.read(1, window=window).tobytes())
        if source_hash.hexdigest() != scratch_hash.hexdigest() or scratch_hash.hexdigest() != measurement["decoded_blocks_sha256"]:
            raise ValueError("Scratch measurement does not preserve authentic decoded pixels")
    graph = Path(config["graph"])
    row, col, height, width = config["window"]
    window = (row, col, height, width)
    image = config["reference_image"]
    before = reference_identity(args.scratch, graph, window, image)
    args.output.mkdir(parents=True)
    execution_files = sorted(Path("src/cryolens/preprocess").glob("*.py")) + [
        Path(__file__), config_path, graph
    ]
    execution_hashes = {p.as_posix(): file_digest(p) for p in execution_files}
    with zipfile.ZipFile(args.output / "executed-code.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in execution_files:
            archive.write(path, path.relative_to(Path.cwd()) if path.is_absolute() else path)
    prefix = args.output / "reference/short-block"
    started_at = datetime.now(UTC).isoformat()
    print("Executing one fresh SNAP 14 short-block reference", flush=True)
    run_reference(args.scratch, graph, prefix, window, image)
    verify_reference_receipt(args.scratch, graph, prefix, window, image)
    if reference_identity(args.scratch, graph, window, image) != before:
        raise ValueError("Consumed reference inputs changed during execution")
    reader = SAFEProductReader(args.safe)
    checks = {}
    for pol in ("HH", "HV"):
        checks["calibration_" + pol] = compare_power(
            reader.read_sigma0(pol, False, window=window)["sigma0_linear"],
            read_reference(prefix, "raw", "Sigma0_" + pol),
        )
        checks["noise_" + pol] = compare_power(
            reader.read_sigma0(pol, window=window)["sigma0_linear"],
            read_reference(prefix, "den", "sigma0_" + pol.lower()),
        )
    receipt_path = Path(str(prefix) + "-receipt.json")
    report = {
        "schema_version": 1,
        "recipe_id": config["recipe_id"],
        "configuration_sha256": file_digest(config_path),
        "graph_sha256": file_digest(graph),
        "reference_image_id": before["image_id"],
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "execution_source_sha256": execution_hashes,
        "execution_snapshot_sha256": file_digest(args.output / "executed-code.zip"),
        "fresh_snap_runs_in_this_report": 1,
        "scientific_comparison_passed": all(check["passed"] for check in checks.values()),
        "deployment_gate_open": False,
        "previous_reference_status": "invalidated; preserved unchanged",
        "previous_reference_receipt_sha256": file_digest(Path("docs/processing-validation/v2/reference-receipts/short-block-receipt.json")),
        "previous_invalidation_sha256": file_digest(Path("docs/processing-validation/v2/reference-inventory-audit.json")),
        "products": [
            {
                "product": args.safe.name,
                "mode": "EW",
                "provenance": {
                    "live_source_files_sha256": actual,
                    "scratch_decoded_pixels_verified_against_authentic_source": True,
                    "reference_receipt_sha256": file_digest(receipt_path),
                },
                "window": list(window),
                "signed_checks": checks,
                "quality": baseline["full_quality"],
                "quality_reused_from_historical_report_sha256": file_digest(baseline_path),
                "quality_original_observed_at": baseline["observed_at"],
                "quality_rescanned_in_this_report": False,
            }
        ],
        "claim_boundary": "Fresh authentic short-block calibration and signed-noise comparison only. Historical whole-product radiometric rejection is reused with its original generation provenance; no fresh full scan, geometry acceptance, held-out evaluation, human review or deployment readiness is claimed.",
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    args.publish.mkdir(parents=True)
    (args.publish / "reference-receipts").mkdir()
    for source, destination in (
        (args.output / "report.json", args.publish / "report.json"),
        (args.output / "executed-code.zip", args.publish / "executed-code.zip"),
        (receipt_path, args.publish / "reference-receipts/short-block-receipt.json"),
    ):
        shutil.copyfile(source, destination)
    print(json.dumps(checks), flush=True)
    if not report["scientific_comparison_passed"]:
        raise RuntimeError("Fresh short-block comparison failed unchanged thresholds")


if __name__ == "__main__":
    main()
