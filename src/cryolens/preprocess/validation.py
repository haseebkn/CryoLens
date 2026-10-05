"""Reproducible matched-pixel SAFE validation against an external SNAP runtime."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Geod
from rasterio.enums import Resampling
from rasterio.shutil import copy as copy_raster
from rasterio.warp import reproject
from rio_cogeo.cogeo import cog_validate
from skimage.registration import phase_cross_correlation

from cryolens.eval.cohort import acquisition_id, file_digest, load_manifest
from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.quality import ProcessingQualityError, assess_safe_product
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path, _parse_product_annotation
from cryolens.preprocess.stack import COGStackBuilder


def require_development_product(safe: Path, manifest: Path) -> None:
    """Refuse analytic inspection of a frozen test acquisition or its variants."""
    sealed = load_manifest(manifest)
    # load_manifest returns the validated payload.
    records = sealed["scenes"]
    original = acquisition_id(safe.name)
    if any(r["acquisition_id"] == original and r["partition"] == "test" for r in records):
        raise ValueError("Frozen test acquisition cannot be used for processing development")


def ipf_version(safe: Path) -> str:
    """Read the named radar IPF rather than the first software (often COGifier)."""
    root = ET.parse(_open_path(safe / "manifest.safe")).getroot()
    versions = {
        v.get("version", "")
        for v in root.iter()
        if v.tag.split("}")[-1] == "software" and v.get("name") == "Sentinel-1 IPF"
    }
    if len(versions) != 1:
        raise ValueError("Exactly one unambiguous named Sentinel-1 IPF version is required")
    return versions.pop()


def prepare_reference_input(safe: Path, output: Path) -> dict[str, Any]:
    """Lossless scratch view with explicit COG/IPF provenance adaptation.

    Original inputs are never modified. The scratch manifest is not an original
    authenticated SAFE archive and its pre-conversion checksum claims are not
    used as integrity evidence. Hashes below bind every decoded measurement.
    """
    import shutil

    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        _open_path(safe / "annotation"), _open_path(output / "annotation"), dirs_exist_ok=True
    )
    (output / "measurement").mkdir(exist_ok=True)
    receipts = []
    for source in sorted((safe / "measurement").glob("*.tiff")):
        target = output / "measurement" / source.name
        if not target.exists():
            copy_raster(
                _open_path(source),
                _open_path(target),
                driver="GTiff",
                compress="DEFLATE",
                tiled=True,
                blockxsize=256,
                blockysize=256,
                BIGTIFF="YES",
            )
        original_hash, decoded_hash = hashlib.sha256(), hashlib.sha256()
        with (
            rasterio.open(_open_path(source)) as original,
            rasterio.open(_open_path(target)) as converted,
        ):
            if original.shape != converted.shape or original.dtypes != converted.dtypes:
                raise ValueError("Reference scratch measurement changed dimensions/type")
            for _, window in converted.block_windows(1):
                original_hash.update(original.read(1, window=window).tobytes())
                decoded_hash.update(converted.read(1, window=window).tobytes())
        if original_hash.digest() != decoded_hash.digest():
            raise ValueError("Reference scratch measurement changed decoded pixels")
        receipts.append(
            {
                "measurement": source.name,
                "source_sha256": file_digest(source),
                "decoded_blocks_sha256": original_hash.hexdigest(),
                "decoded_pixels_identical": True,
            }
        )
    manifest = safe / "manifest.safe"
    for _, namespace in ET.iterparse(_open_path(manifest), events=["start-ns"]):
        if namespace[0]:
            ET.register_namespace(*namespace)
    tree = ET.parse(_open_path(manifest))
    adapted = False
    for parent in tree.getroot().iter():
        for child in list(parent):
            if child.tag.split("}")[-1] == "processing" and child.get("name") == "COG Conversion":
                original = next(
                    (
                        v
                        for v in child.iter()
                        if v.tag.split("}")[-1] == "processing"
                        and v.get("name") == "GRD Post Processing"
                    ),
                    None,
                )
                if original is None:
                    raise ValueError("COG conversion lacks original GRD processing provenance")
                parent.remove(child)
                parent.append(copy.deepcopy(original))
                adapted = True
    tree.write(_open_path(output / "manifest.safe"), encoding="utf-8", xml_declaration=True)
    return {
        "product": safe.name,
        "ipf_version": ipf_version(safe),
        "manifest_adapter_applied": adapted,
        "adapter": "Select existing GRD Post Processing record instead of outer COG Conversion wrapper",
        "original_manifest_sha256": file_digest(manifest),
        "scratch_manifest_sha256": file_digest(output / "manifest.safe"),
        "measurements": receipts,
        "annotation_sha256": {
            p.relative_to(safe).as_posix(): file_digest(p)
            for p in sorted((safe / "annotation").rglob("*.xml"))
        },
    }


def compare_power(
    actual: np.ndarray, reference: np.ndarray, min_pixels: int = 4096
) -> dict[str, Any]:
    """Matched residual signs and positive-power error; empty comparisons fail."""
    if actual.shape != reference.shape:
        raise ValueError("Reference and actual source-pixel grids differ")
    finite = np.isfinite(actual) & np.isfinite(reference)
    positive = finite & (actual > 0) & (reference > 0)
    count = int(positive.sum())
    if count < min_pixels:
        return {
            "passed": False,
            "positive_matched_pixels": count,
            "reason": "insufficient matched pixels",
        }
    delta = 10 * np.log10(actual[positive].astype(np.float64) / reference[positive])
    sign_mismatch = float(np.mean((actual[finite] > 0) != (reference[finite] > 0)))
    p95 = float(np.percentile(np.abs(delta), 95))
    return {
        "positive_matched_pixels": count,
        "finite_matched_pixels": int(finite.sum()),
        "median_bias_db": float(np.median(delta)),
        "p95_absolute_error_db": p95,
        "sign_mismatch_fraction": sign_mismatch,
        "passed": p95 <= 0.02 and sign_mismatch <= 0.0001,
    }


def read_reference(prefix: Path, stage: str, band: str) -> np.ndarray:
    with rasterio.open(str(prefix) + f"_{stage}.data/{band}.img") as source:
        return np.asarray(source.read(1))


def measure_registration(
    actual: np.ndarray, reference: np.ndarray, valid: np.ndarray, pixel_size_m: float
) -> dict[str, Any]:
    """Measure relative spatial displacement independently of median radiometry."""
    if (
        np.count_nonzero(valid) < 4096
        or np.std(actual[valid]) < 1e-10
        or np.std(reference[valid]) < 1e-10
    ):
        return {"passed": False, "reason": "insufficient spatial texture/overlap"}
    shift, _, _ = phase_cross_correlation(
        np.where(valid, actual, 0),
        np.where(valid, reference, 0),
        reference_mask=valid,
        moving_mask=valid,
        overlap_ratio=0.8,
    )
    displacement = float(np.hypot(*shift) * pixel_size_m)
    return {
        "shift_rows_px": float(shift[0]),
        "shift_columns_px": float(shift[1]),
        "relative_displacement_m": displacement,
        "passed": displacement <= 40,
    }


def compare_window(
    reader: SAFEProductReader, window: tuple[int, int, int, int], prefix: Path, output: Path
) -> dict[str, Any]:
    """Compare calibration/noise in sensor coordinates and warps on a common grid."""
    raw = {p: reader.read_sigma0(p, False, window=window) for p in ("HH", "HV")}
    corrected = {p: reader.read_sigma0(p, window=window) for p in ("HH", "HV")}
    checks: dict[str, Any] = {}
    for pol in ("HH", "HV"):
        checks[f"calibration_{pol}"] = compare_power(
            raw[pol]["sigma0_linear"], read_reference(prefix, "raw", f"Sigma0_{pol}")
        )
        checks[f"noise_{pol}"] = compare_power(
            corrected[pol]["sigma0_linear"], read_reference(prefix, "den", f"sigma0_{pol.lower()}")
        )
    latitude = read_reference(prefix, "den", "latitude_band")
    longitude = read_reference(prefix, "den", "longitude_band")
    _, _, distance = Geod(ellps="WGS84").inv(
        raw["HH"]["longitude"], raw["HH"]["latitude"], longitude, latitude
    )
    angle_error = np.abs(
        raw["HH"]["incidence_angle_deg"] - read_reference(prefix, "den", "incidence")
    )
    checks["annotation_geolocation"] = {
        "max_error_m": float(np.max(distance)),
        "passed": bool(np.isfinite(distance).all() and np.max(distance) <= 20),
    }
    checks["incidence"] = {
        "max_error_deg": float(np.max(angle_error)),
        "passed": bool(np.isfinite(angle_error).all() and np.max(angle_error) <= 0.05),
    }
    # Raw calibrated power isolates geocoding from signed denoising failures.
    # This diagnostic COG is explicitly rejected for detection.
    hh, hv = raw["HH"], raw["HV"]
    with rasterio.open(str(prefix) + "_geo.tif") as reference_grid:
        if reference_grid.crs != rasterio.crs.CRS.from_epsg(3978):
            raise ValueError("Independent geocoding reference must have explicit EPSG:3978")
        destination_grid = (reference_grid.transform, reference_grid.width, reference_grid.height)
    result = PurePythonSARProcessor().process_calibrated_arrays(
        hh["sigma0_linear"],
        hv["sigma0_linear"],
        hh["incidence_angle_deg"],
        hh["latitude"],
        hh["longitude"],
        destination_grid=destination_grid,
    )
    cog = COGStackBuilder(output).build_and_export_cog(
        prefix.name,
        result["bands"],
        result["transform"],
        provenance={
            "source_kind": "sentinel1_safe",
            "processing_quality": "rejected",
            "purpose": "uncorrected-power geocoding diagnostic; never detection",
        },
    )
    is_valid, errors, warnings = cog_validate(str(cog))
    checks["cog_integrity"] = {
        "passed": bool(is_valid),
        "errors": errors,
        "warnings": warnings,
        "sha256": file_digest(cog),
    }
    for pol in ("HH", "HV"):
        with rasterio.open(str(prefix) + "_geo.tif") as source:
            reference = np.full(result["shape"], np.nan, dtype=np.float32)
            reproject(
                source=rasterio.band(source, 1 if pol == "HH" else 2),
                destination=reference,
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=0,
                dst_transform=result["transform"],
                dst_crs=result["crs"],
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
        ours = result["bands"][f"sigma0_{pol.lower()}_db"]
        valid = np.isfinite(reference) & (reference > 0) & (ours != result["nodata"])
        # Exclude two edge pixels where independent footprints/resampling differ.
        valid[:2] = valid[-2:] = False
        valid[:, :2] = valid[:, -2:] = False
        difference = ours[valid] - 10 * np.log10(reference[valid])
        checks[f"registration_{pol}"] = measure_registration(
            ours, 10 * np.log10(np.maximum(reference, 1e-20)), valid, abs(result["transform"].a)
        )
        checks[f"reprojection_{pol}"] = {
            "matched_pixels": int(valid.sum()),
            "median_bias_db": float(np.median(difference)) if difference.size else None,
            "p95_absolute_error_db": float(np.percentile(abs(difference), 95))
            if difference.size
            else None,
            "passed": bool(difference.size >= 4096 and np.percentile(abs(difference), 95) <= 1.0),
        }
    checks["diagnostic_scope"] = {
        "passed": True,
        "thermal_noise_corrected": False,
        "fresh_detection_allowed": False,
    }
    return {
        "window": window,
        "checks": checks,
        "scientific_comparison_passed": all(c["passed"] for c in checks.values()),
        "nonpositive_fraction": {
            p: corrected[p]["nonpositive_power_fraction"] for p in ("HH", "HV")
        },
    }


def run_reference(
    scratch: Path, graph: Path, prefix: Path, window: tuple[int, int, int, int], image: str
) -> None:
    """Invoke the actual external processor, retain output and fail on exit/errors."""
    prefix.parent.mkdir(parents=True, exist_ok=True)
    r, c, h, w = window
    command = [
        "docker",
        "run",
        "--rm",
        "--memory",
        "6g",
        "-v",
        f"{scratch.parent.resolve().as_posix()}:/input",
        "-v",
        f"{graph.parent.resolve().as_posix()}:/graphs:ro",
        "-v",
        f"{prefix.parent.resolve().as_posix()}:/output",
        image,
        "/graphs/" + graph.name,
        "-Pinput_file=/input/" + scratch.name + "/manifest.safe",
        f"-Pregion={c},{r},{w},{h}",
        "-Poutput_prefix=/output/" + prefix.name,
        "-q",
        "2",
        "-c",
        "512M",
        "-J-Xmx4G",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    log = result.stdout + result.stderr
    Path(str(prefix) + ".log").write_text(log, encoding="utf-8")
    if result.returncode or "Error:" in log or "StackOverflowError" in log:
        raise RuntimeError("Independent SNAP run failed; see " + str(prefix) + ".log")
    write_reference_receipt(scratch, graph, prefix, window, image)


def reference_identity(
    scratch: Path, graph: Path, window: tuple[int, int, int, int], image: str
) -> dict[str, Any]:
    """Bind cached reference pixels to inputs, recipe, runtime and source window."""
    runtime = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    return {
        "window": list(window),
        "image_id": runtime,
        "graph_sha256": file_digest(graph),
        "scratch_files_sha256": {
            path.relative_to(scratch).as_posix(): file_digest(path)
            for path in sorted(scratch.rglob("*"))
            if path.is_file() and path.suffix.lower() in {".safe", ".xml", ".tiff"}
        },
    }


def write_reference_receipt(
    scratch: Path, graph: Path, prefix: Path, window: tuple[int, int, int, int], image: str
) -> None:
    receipt = reference_identity(scratch, graph, window, image)
    outputs = [Path(str(prefix) + "_geo.tif")]
    for stage in ("raw", "den"):
        outputs.append(Path(str(prefix) + f"_{stage}.dim"))
        outputs.extend(sorted(Path(str(prefix) + f"_{stage}.data").rglob("*.img")))
    if len(outputs) < 9 or not all(path.is_file() for path in outputs):
        raise ValueError("Independent reference outputs are incomplete")
    receipt["outputs_sha256"] = {
        path.relative_to(prefix.parent).as_posix(): file_digest(path) for path in outputs
    }
    Path(str(prefix) + "-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def verify_reference_receipt(
    scratch: Path, graph: Path, prefix: Path, window: tuple[int, int, int, int], image: str
) -> None:
    path = Path(str(prefix) + "-receipt.json")
    if not path.is_file():
        raise ValueError("Reference cache lacks provenance; regenerate with --run-reference")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    identity = reference_identity(scratch, graph, window, image)
    if any(receipt.get(key) != value for key, value in identity.items()):
        raise ValueError("Reference cache input/graph/runtime/window changed; regenerate reference")
    for name, expected in receipt["outputs_sha256"].items():
        target = (prefix.parent / name).resolve()
        if not target.is_relative_to(prefix.parent.resolve()):
            raise ValueError("Reference receipt contains an invalid output path")
        if not target.is_file() or file_digest(target) != expected:
            raise ValueError("Reference output missing, ambiguous or changed: " + name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/sentinel1-validation-v1.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--reference-image", default="cryolens-snap-reference:14.0.0")
    parser.add_argument("--run-reference", action="store_true")
    parser.add_argument(
        "--reuse-full-scan",
        action="store_true",
        help="Reuse a completed full scan only when input, reader and quality-code hashes match",
    )
    parser.add_argument(
        "--skip-full-scan",
        action="store_true",
        help="Diagnostic rerun only; cannot open deployment gate",
    )
    args = parser.parse_args()
    previous = None
    previous_sha256 = None
    if args.reuse_full_scan:
        previous_path = args.output_dir / "report.json"
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_sha256 = file_digest(previous_path)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_info = subprocess.run(
        ["docker", "image", "inspect", args.reference_image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    report: dict[str, Any] = {
        "schema_version": 1,
        "configuration_sha256": file_digest(args.config),
        "graph_sha256": file_digest(Path(config["graph"])),
        "reference_image_id": image_info,
        "reference_image": args.reference_image,
        "validation_source_sha256": {
            name: file_digest(Path(__file__).parent / name)
            for name in (
                "validation.py",
                "quality.py",
                "safe_reader.py",
                "python_chain.py",
                "stack.py",
            )
        },
        "scientific_comparison_passed": False,
        "deployment_gate_open": False,
        "products": [],
    }
    for item in config["products"]:
        safe = Path(item["path"])
        require_development_product(safe, Path(config["frozen_manifest"]))
        reader = SAFEProductReader(safe)
        receipt = prepare_reference_input(safe, args.scratch_dir / safe.name)
        product: dict[str, Any] = {
            "product": safe.name,
            "mode": reader.instrument_mode,
            "provenance": receipt,
            "windows": [],
        }
        print("Validating " + safe.name, flush=True)
        if previous is not None:
            for name in ("safe_reader.py", "quality.py"):
                if (
                    previous["validation_source_sha256"][name]
                    != report["validation_source_sha256"][name]
                ):
                    raise ValueError("Cached full scan reader/quality code changed; rescan product")
            cached = next(p for p in previous["products"] if p["product"] == safe.name)
            if (
                cached["provenance"] != receipt
                or cached["quality"].get("scope") != "full_measurement_grid"
            ):
                raise ValueError("Cached full scan inputs/scope changed; rescan product")
            product["quality"] = copy.deepcopy(cached["quality"])
            product["full_scan_reused_from_report_sha256"] = previous_sha256
        elif not args.skip_full_scan:
            try:
                product["quality"] = assess_safe_product(
                    reader, args.output_dir / f"{item['name']}-full-quality.json"
                )
            except ProcessingQualityError as exc:
                product["quality"] = exc.report
        else:
            product["quality"] = {"passed": False, "reasons": ["Full measurement scan skipped"]}
        meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
        size = config["window_size_px"]
        for row_fraction, col_fraction in config["sampling_fractions"]:
            window = (
                round((meta["n_lines"] - size) * row_fraction),
                round((meta["n_samples"] - size) * col_fraction),
                size,
                size,
            )
            prefix = (
                args.output_dir / "reference" / f"{item['name']}-r{row_fraction}-c{col_fraction}"
            )
            try:
                if args.run_reference:
                    run_reference(
                        args.scratch_dir / safe.name,
                        Path(config["graph"]),
                        prefix,
                        window,
                        args.reference_image,
                    )
                verify_reference_receipt(
                    args.scratch_dir / safe.name,
                    Path(config["graph"]),
                    prefix,
                    window,
                    args.reference_image,
                )
                comparison = compare_window(reader, window, prefix, args.output_dir / "diagnostics")
                comparison["reference_receipt_sha256"] = file_digest(
                    Path(str(prefix) + "-receipt.json")
                )
                product["windows"].append(comparison)
            except (ValueError, RuntimeError, OSError, rasterio.errors.RasterioError) as exc:
                product["windows"].append(
                    {"window": window, "scientific_comparison_passed": False, "reason": str(exc)}
                )
        product["scientific_comparison_passed"] = all(
            w["scientific_comparison_passed"] for w in product["windows"]
        )
        report["products"].append(product)
        report["scientific_comparison_passed"] = all(
            p["scientific_comparison_passed"] for p in report["products"]
        )
        report["deployment_gate_open"] = (
            len(report["products"]) == len(config["products"])
            and report["scientific_comparison_passed"]
            and all(p["quality"]["passed"] for p in report["products"])
        )
        (args.output_dir / "report.json").write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )
    return 0 if report["deployment_gate_open"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
