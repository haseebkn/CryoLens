"""Execute and verify corrected EW/IW complete outputs against real SNAP."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Geod
from rasterio.windows import from_bounds

from cryolens.eval.cohort import file_digest
from cryolens.eval.tracking import code_provenance
from cryolens.preprocess.product import (
    NODATA,
    execution_context,
    inspect_corrected_output,
    repackage_complete_output,
    verify_pixel_identity,
    write_record,
)
from cryolens.preprocess.provenance import original_inventory, safe_source_inventory
from cryolens.preprocess.quality import QualityPolicy
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path, _parse_product_annotation
from cryolens.preprocess.validation import (
    compare_power,
    measure_registration,
    read_reference,
    reference_input_files,
    require_development_product,
    run_reference,
    verify_reference_receipt,
)
from validation.finalise_southern_iw_report import full_grid_quality


def bound_sources(item: dict[str, Any], manifest: Path) -> tuple[SAFEProductReader, dict[str, str]]:
    safe = Path(item["path"])
    require_development_product(safe, manifest)
    reader = SAFEProductReader(safe)
    inventory = safe_source_inventory(reader)
    previous = json.loads(Path(item["qualified_report"]).read_text())
    prior = next(p for p in previous["products"] if p["product"] == safe.name)
    if not prior["quality"]["passed"] or not all(
        w["scientific_comparison_passed"] for w in prior["windows"]
    ):
        raise ValueError("Prior product is not a qualified EW/IW example")
    if inventory != original_inventory(prior["provenance"]):
        raise ValueError("Original downloaded source changed since qualification")
    scratch = Path(item["scratch"])
    cache = json.loads(Path(item["prior_receipt"]).read_text())
    actual = {
        p.relative_to(scratch).as_posix(): file_digest(Path(_open_path(p)))
        for p in reference_input_files(scratch)
    }
    if actual != cache["scratch_files_sha256"]:
        raise ValueError("Reference scratch inventory differs from preserved original execution")
    annotations = {k: v for k, v in actual.items() if k.startswith("annotation/")}
    if annotations != prior["provenance"]["annotation_sha256"]:
        raise ValueError("Reference scratch annotations differ from original source")
    return reader, inventory


def windows_for(
    reader: SAFEProductReader, config: dict[str, Any]
) -> list[tuple[int, int, int, int]]:
    meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
    size = config["window_size_px"]
    return [
        (round((meta["n_lines"] - size) * rf), round((meta["n_samples"] - size) * cf), size, size)
        for rf, cf in config["sampling_fractions"]
    ]


def compare_complete_window(
    output: Path, reader: SAFEProductReader, window: tuple[int, int, int, int], prefix: Path
) -> dict[str, Any]:
    """Compare actual corrected full-product pixels on the independently generated grid."""
    checks: dict[str, Any] = {}
    raw = {p: reader.read_sigma0(p, False, window=window) for p in ("HH", "HV")}
    corrected = {p: reader.read_sigma0(p, window=window) for p in ("HH", "HV")}
    for pol in ("HH", "HV"):
        checks["calibration_" + pol] = compare_power(
            raw[pol]["sigma0_linear"], read_reference(prefix, "raw", "Sigma0_" + pol)
        )
        checks["noise_" + pol] = compare_power(
            corrected[pol]["sigma0_linear"], read_reference(prefix, "den", "sigma0_" + pol.lower())
        )
    lat = read_reference(prefix, "den", "latitude_band")
    lon = read_reference(prefix, "den", "longitude_band")
    _, _, distance = Geod(ellps="WGS84").inv(
        raw["HH"]["longitude"], raw["HH"]["latitude"], lon, lat
    )
    angle = abs(raw["HH"]["incidence_angle_deg"] - read_reference(prefix, "den", "incidence"))
    checks["annotation_geolocation"] = {
        "max_error_m": float(distance.max()),
        "passed": bool(np.isfinite(distance).all() and distance.max() <= 20),
    }
    checks["incidence"] = {
        "max_error_deg": float(angle.max()),
        "passed": bool(np.isfinite(angle).all() and angle.max() <= 0.05),
    }
    with rasterio.open(str(prefix) + "_geo.tif") as reference, rasterio.open(output) as actual:
        if reference.crs != actual.crs or reference.res != actual.res:
            raise ValueError("Corrected reference grid CRS or spacing differs")
        target = from_bounds(*reference.bounds, transform=actual.transform)
        if not np.allclose(
            [target.col_off, target.row_off, target.width, target.height],
            np.round([target.col_off, target.row_off, target.width, target.height]),
            atol=1e-6,
            rtol=0,
        ):
            raise ValueError("Complete output must be aligned to the independent reference grid")
        target = target.round_offsets().round_lengths()
        ours = actual.read(window=target, boundless=True, fill_value=NODATA)
        theirs = reference.read()
    if ours.shape[1:] != theirs.shape[1:]:
        raise ValueError("Matched complete-output/reference grid shapes differ")
    for index, pol in enumerate(("HH", "HV")):
        power = theirs[index]
        valid = (
            np.isfinite(power) & (power > 0) & np.isfinite(ours[index]) & (ours[index] != NODATA)
        )
        valid[:2] = valid[-2:] = False
        valid[:, :2] = valid[:, -2:] = False
        difference = ours[index, valid] - 10 * np.log10(power[valid])
        reference_db = np.full(power.shape, NODATA, dtype=np.float32)
        reference_db[valid] = 10 * np.log10(power[valid])
        checks["registration_" + pol] = measure_registration(ours[index], reference_db, valid, 40)
        p95 = float(np.percentile(abs(difference), 95)) if difference.size else None
        checks["corrected_reprojection_" + pol] = {
            "matched_pixels": int(valid.sum()),
            "median_bias_db": float(np.median(difference)) if difference.size else None,
            "p95_absolute_error_db": p95,
            "passed": bool(difference.size >= 4096 and p95 is not None and p95 <= 1),
        }
    return {
        "window": list(window),
        "reference_receipt_sha256": file_digest(Path(str(prefix) + "-receipt.json")),
        "checks": checks,
        "scientific_comparison_passed": all(c["passed"] for c in checks.values()),
        "scope": "Noise-corrected full-product output, not raw-power diagnostic windows",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--stage", choices=["references", "process", "package", "compare"], required=True
    )
    parser.add_argument("--packaged-products", type=Path)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    config = json.loads(args.config.read_text())
    graph = Path(config["graph"])
    root = args.output_dir
    if config["policy"] != QualityPolicy().model_dump():
        raise ValueError("Frozen quality thresholds cannot change")
    if config["window_size_px"] != 512 or config["sampling_fractions"] != [
        [r, c] for r in (0.1, 0.5, 0.9) for c in (0.1, 0.5, 0.9)
    ]:
        raise ValueError("The nine predeclared comparison windows cannot change")
    runtime = subprocess.check_output(
        ["docker", "image", "inspect", config["reference_image"], "--format", "{{.Id}}"], text=True
    ).strip()
    if runtime != config["reference_image_id"]:
        raise ValueError("Independent reference image changed")
    if args.stage == "references":
        root.mkdir(parents=True, exist_ok=False)
        write_record(root / "configuration.json", config)
        execution = code_provenance(Path.cwd())
        write_record(root / "execution-source.json", execution)
        with zipfile.ZipFile(root / "executed-code.zip", "x", zipfile.ZIP_DEFLATED) as archive:
            for name, expected in execution["source_files_sha256"].items():
                if file_digest(Path(name)) != expected:
                    raise ValueError("Source changed during reference snapshot")
                archive.write(name, name)
    elif file_digest(root / "configuration.json") != file_digest(args.config):
        raise ValueError("Validation configuration changed between execution phases")
    publication = args.report_dir or root
    if args.stage == "compare" and (publication / "report.json").exists():
        raise FileExistsError("Preserve prior completed scientific evidence")
    comparison_execution = None
    if args.stage == "compare":
        snapshot_root = publication / "comparison-execution"
        snapshot_root.mkdir(parents=True, exist_ok=False)
        comparison_execution, payloads = execution_context()
        write_record(snapshot_root / "execution-source.json", comparison_execution)
        with zipfile.ZipFile(
            snapshot_root / "executed-code.zip", "x", zipfile.ZIP_DEFLATED
        ) as archive:
            for name, content in payloads.items():
                archive.writestr(name, content)
    rows = []
    for item in config["products"]:
        print(args.stage, item["name"], flush=True)
        reader, inventory = bound_sources(item, Path(config["frozen_manifest"]))
        windows = windows_for(reader, config)
        reference_root = root / "reference" / item["name"]
        prefixes = [
            reference_root / f"{item['name']}-r{rf}-c{cf}"
            for rf, cf in config["sampling_fractions"]
        ]
        if args.stage == "references":
            for window, prefix in zip(windows, prefixes, strict=True):
                print("Executing corrected SNAP reference", prefix.name, flush=True)
                run_reference(
                    Path(item["scratch"]), graph, prefix, window, config["reference_image"]
                )
        elif args.stage == "process":
            command = [
                sys.executable,
                "-m",
                "cryolens.preprocess",
                "--engine",
                "python",
                "--scene",
                item["path"],
                "--output-dir",
                str(root / "products"),
                "--grid-reference",
                str(prefixes[0]) + "_geo.tif",
                "--block-size",
                str(config["block_size"]),
                "--scan-rows",
                str(config["scan_rows"]),
            ]
            subprocess.run(command, check=True)
        elif args.stage == "package":
            if args.packaged_products is None:
                raise ValueError("Packaging needs a new packaged-products root")
            original = root / "products" / reader.safe_dir.stem
            receipt = json.loads((original / "processing-receipt.json").read_text())
            repackage_complete_output(
                original / receipt["output"], args.packaged_products / reader.safe_dir.stem
            )
        else:
            product_root = root / "products" / reader.safe_dir.stem
            receipt = json.loads((product_root / "processing-receipt.json").read_text())
            output = product_root / receipt["output"]
            if (
                receipt["source_files_sha256"] != inventory
                or file_digest(output) != receipt["output_sha256"]
            ):
                raise ValueError("Complete processing receipt source/output binding differs")
            for name, key in [
                ("source-quality.json", "source_quality_sha256"),
                ("configuration.json", "configuration_sha256"),
                ("output-quality.json", "output_quality_sha256"),
                ("execution-source.json", "execution_source_sha256"),
                ("executed-code.zip", "execution_snapshot_sha256"),
            ]:
                if file_digest(product_root / name) != receipt[key]:
                    raise ValueError("Complete processing receipt changed: " + name)
            source_quality = json.loads((product_root / "source-quality.json").read_text())
            full_grid_quality(source_quality, reader.safe_dir.name, reader.instrument_mode)
            if not source_quality["passed"]:
                raise ValueError("Full source quality rejected")
            execution = json.loads((product_root / "execution-source.json").read_text())
            with zipfile.ZipFile(product_root / "executed-code.zip") as archive:
                for name, expected in execution["source_files_sha256"].items():
                    if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                        raise ValueError("Processing execution source snapshot changed")
            packaging_sha256 = None
            if args.packaged_products is not None:
                packaged = args.packaged_products / reader.safe_dir.stem
                packaging = json.loads((packaged / "packaging-receipt.json").read_text())
                if (
                    packaging["original_output_sha256"] != receipt["output_sha256"]
                    or packaging["original_processing_receipt_sha256"]
                    != file_digest(product_root / "processing-receipt.json")
                    or not packaging["all_corrected_pixels_identical"]
                ):
                    raise ValueError(
                        "Replacement packaging is not bound to original complete processing"
                    )
                original_output = output
                output = packaged / packaging["output"]
                if file_digest(output) != packaging["output_sha256"]:
                    raise ValueError("Replacement packaged output changed")
                count = verify_pixel_identity(original_output, output)
                if count != packaging["full_resolution_pixels_compared"]:
                    raise ValueError("Replacement packaging pixel traversal incomplete")
                for name, key in [
                    ("output-quality.json", "output_quality_sha256"),
                    ("execution-source.json", "execution_source_sha256"),
                    ("executed-code.zip", "execution_snapshot_sha256"),
                ]:
                    if file_digest(packaged / name) != packaging[key]:
                        raise ValueError("Replacement packaging receipt changed: " + name)
                packaging_sha256 = file_digest(packaged / "packaging-receipt.json")
            actual_quality = inspect_corrected_output(output)
            comparisons = []
            for window, prefix in zip(windows, prefixes, strict=True):
                verify_reference_receipt(
                    Path(item["scratch"]), graph, prefix, window, config["reference_image"]
                )
                comparison = compare_complete_window(output, reader, window, prefix)
                comparisons.append(comparison)
                print(prefix.name, comparison["scientific_comparison_passed"], flush=True)
            accepted = actual_quality["passed"] and all(
                w["scientific_comparison_passed"] for w in comparisons
            )
            rows.append(
                {
                    "product": reader.safe_dir.name,
                    "mode": reader.instrument_mode,
                    "source_files_sha256": inventory,
                    "processing_receipt_sha256": file_digest(
                        product_root / "processing-receipt.json"
                    ),
                    "output_sha256": file_digest(output),
                    "cog_packaging_receipt_sha256": packaging_sha256,
                    "output_quality": actual_quality,
                    "source_quality": json.loads(
                        (product_root / "source-quality.json").read_text()
                    ),
                    "windows": comparisons,
                    "representative_preprocessing_passed": accepted,
                }
            )
    if args.stage == "compare":
        complete = (
            len(rows) == 2
            and {p["mode"] for p in rows} == {"EW", "IW"}
            and all(p["representative_preprocessing_passed"] for p in rows)
        )
        publication.mkdir(parents=True, exist_ok=True)
        if comparison_execution is None:
            raise ValueError("Comparison execution provenance missing")
        if (
            execution_context()[0]["source_files_sha256"]
            != comparison_execution["source_files_sha256"]
        ):
            raise ValueError("Comparison code changed during execution")
        write_record(
            publication / "report.json",
            {
                "schema_version": 1,
                "observed_at": datetime.now(UTC).isoformat(),
                "configuration_sha256": file_digest(args.config),
                "comparison_execution_source_sha256": file_digest(
                    publication / "comparison-execution/execution-source.json"
                ),
                "comparison_execution_snapshot_sha256": file_digest(
                    publication / "comparison-execution/executed-code.zip"
                ),
                "graph_sha256": file_digest(graph),
                "reference_image_id": runtime,
                "policy": QualityPolicy().model_dump(),
                "products": rows,
                "step2_complete": complete,
                "deployment_gate_open": False,
                "scientific_release_granted": False,
                "claim_boundary": "Representative complete corrected EW/IW preprocessing only. Seasonal/regional availability, absolute field accuracy, target evaluation, aligned sea-ice context and operational deployment remain outside this milestone.",
            },
        )
        return 0 if complete else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
