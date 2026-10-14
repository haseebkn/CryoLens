"""Complete corrected SAFE products with bounded raster and coordinate allocations."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.shutil import copy as copy_raster
from rasterio.transform import from_origin
from rasterio.windows import Window
from rio_cogeo.cogeo import cog_validate
from scipy.ndimage import map_coordinates

from cryolens.eval.cohort import digest, file_digest
from cryolens.eval.tracking import code_provenance
from cryolens.preprocess.marine_geocoding import MarineGeocoder
from cryolens.preprocess.provenance import safe_source_inventory
from cryolens.preprocess.quality import QualityPolicy, assess_safe_product
from cryolens.preprocess.safe_reader import SAFEProductReader, _parse_product_annotation
from cryolens.preprocess.stack import BAND_NAMES

logger = logging.getLogger(__name__)
NODATA = -9999.0


def write_record(path: Path, value: dict[str, Any]) -> None:
    """Scientific records are new byte contracts, never replacement snapshots."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def execution_context() -> tuple[dict[str, Any], dict[str, bytes]]:
    """Capture actual executing bytes in source checkouts and installed packages."""
    project = Path(__file__).resolve().parents[3]
    if (project / "src/cryolens/preprocess/product.py").resolve() == Path(__file__).resolve():
        execution = code_provenance(project)
        payloads = {
            name: (project / name).read_bytes() for name in execution["source_files_sha256"]
        }
    else:
        package = Path(__file__).resolve().parents[1]
        payloads = {
            "src/cryolens/" + path.relative_to(package).as_posix(): path.read_bytes()
            for path in sorted(package.rglob("*.py"))
        }
        resources = package / "resources/configs"
        if resources.is_dir():
            payloads.update(
                {
                    "configs/" + path.relative_to(resources).as_posix(): path.read_bytes()
                    for path in sorted(resources.rglob("*"))
                    if path.is_file() and path.suffix in {".json", ".yaml", ".geojson", ".xml"}
                }
            )
        execution = {
            "git_commit": None,
            "git_dirty": None,
            "context": "installed_package",
            "source_files_sha256": {
                name: hashlib.sha256(content).hexdigest() for name, content in payloads.items()
            },
        }
        execution["code_sha256"] = digest(execution["source_files_sha256"])
    if not payloads or any(
        hashlib.sha256(payloads[name]).hexdigest() != expected
        for name, expected in execution["source_files_sha256"].items()
    ):
        raise ValueError("Executing source bytes changed during capture")
    return execution, payloads


def export_complete_cog(source: Path, target: Path) -> None:
    """Use explicit BigTIFF for base and freshly generated nearest overviews.

    The GDAL COG driver avoids rio-cogeo's uncompressed classic-TIFF
    temporary whose full-resolution data plus overviews can exceed 4 GiB.
    """
    if target.exists():
        raise FileExistsError("Preserve existing COG outputs")
    with rasterio.Env(GDAL_CACHEMAX=64 * 1024 * 1024):
        copy_raster(
            source,
            target,
            driver="COG",
            BIGTIFF="YES",
            COMPRESS="DEFLATE",
            PREDICTOR="FLOATING_POINT",
            BLOCKSIZE=512,
            NUM_THREADS="1",
            OVERVIEWS="IGNORE_EXISTING",
            OVERVIEW_COUNT=4,
            RESAMPLING="NEAREST",
            OVERVIEW_RESAMPLING="NEAREST",
        )


def inspect_overviews(path: Path) -> dict[str, Any]:
    """Decode every stored overview pixel in bounded blocks, not just its header."""
    with rasterio.open(path) as base:
        scales = base.overviews(1)
        if any(base.overviews(band) != scales for band in base.indexes):
            raise ValueError("Overview levels differ between bands")
    rows = []
    for index, scale in enumerate(scales):
        counts = {
            "pixels": 0,
            "valid_pixels": 0,
            "mismatched_nodata_pixels": 0,
            "nonfinite_values": 0,
            "inconsistent_ratio_pixels": 0,
            "invalid_incidence_pixels": 0,
        }
        with rasterio.open(path, overview_level=index) as src:
            for _, window in src.block_windows(1):
                data = src.read(window=window)
                masks = data != NODATA
                common = masks.all(axis=0)
                counts["pixels"] += common.size
                counts["valid_pixels"] += int(common.sum())
                counts["mismatched_nodata_pixels"] += int(
                    np.count_nonzero(masks.any(axis=0) != common)
                )
                counts["nonfinite_values"] += int(np.count_nonzero(~np.isfinite(data)))
                counts["inconsistent_ratio_pixels"] += int(
                    np.count_nonzero(
                        abs(data[2, common] - (data[0, common] - data[1, common])) > 0.001
                    )
                )
                counts["invalid_incidence_pixels"] += int(
                    np.count_nonzero((data[3, common] < 15) | (data[3, common] > 50))
                )
            rows.append({"scale": scale, "shape": list(src.shape), **counts})
    return {
        "levels": rows,
        "passed": bool(rows)
        and all(
            not row[key]
            for row in rows
            for key in [
                "mismatched_nodata_pixels",
                "nonfinite_values",
                "inconsistent_ratio_pixels",
                "invalid_incidence_pixels",
            ]
        ),
    }


def product_grid(
    reader: SAFEProductReader, spacing: float = 40, grid_reference: Path | None = None
) -> tuple[Any, int, int]:
    """Cover all annotated footprint knots with a declared two-pixel edge margin.

    Annotation knots define only the extent; the inverse range-Doppler model
    determines every actual source coordinate. No affine/GCP warp is used.
    """
    if not np.isfinite(spacing) or spacing <= 0:
        raise ValueError("Pixel spacing must be finite and positive")
    meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
    xx, yy = Transformer.from_crs(4326, 3978, always_xy=True).transform(
        meta["longitude"], meta["latitude"]
    )
    if not np.isfinite(xx).all() or not np.isfinite(yy).all():
        raise ValueError("Nonfinite projected footprint")
    x_phase = y_phase = 0.0
    if grid_reference is not None:
        with rasterio.open(grid_reference) as reference:
            t = reference.transform
            if (
                reference.crs != rasterio.crs.CRS.from_epsg(3978)
                or t.a != spacing
                or t.e != -spacing
                or t.b
                or t.d
            ):
                raise ValueError("Reference grid must be north-up EPSG:3978 at the fixed spacing")
            x_phase, y_phase = t.c % spacing, t.f % spacing
    left = math.floor((float(np.min(xx)) - x_phase) / spacing) * spacing + x_phase - 2 * spacing
    top = math.ceil((float(np.max(yy)) - y_phase) / spacing) * spacing + y_phase + 2 * spacing
    right = math.ceil((float(np.max(xx)) - x_phase) / spacing) * spacing + x_phase + 2 * spacing
    bottom = math.floor((float(np.min(yy)) - y_phase) / spacing) * spacing + y_phase - 2 * spacing
    return (
        from_origin(left, top, spacing, spacing),
        round((right - left) / spacing),
        round((top - bottom) / spacing),
    )


def corrected_tile(
    reader: SAFEProductReader,
    mapper: MarineGeocoder,
    transform: Any,
    window: Window,
    maximum_source_pixels: int = 4_000_000,
) -> tuple[np.ndarray, dict[str, int]]:
    """Sample corrected linear power with all four bilinear neighbors valid.

    Non-positive source observations remain in the full quality scan. They
    have no logarithm and are excluded explicitly here, without any floor.
    A one-pixel halo prevents tile boundaries changing interpolation support.
    """
    height, width = int(window.height), int(window.width)
    rr, cc = np.indices((height, width), dtype=np.float64)
    xx, yy = transform * (cc + window.col_off + 0.5, rr + window.row_off + 0.5)
    lon, lat = Transformer.from_crs(3978, 4326, always_xy=True).transform(xx, yy)
    rows, cols, supported = mapper.coordinates(lon, lat, (0, 0, *mapper.full_shape))
    counts = {
        "destination_pixels": height * width,
        "geometry_supported": int(supported.sum()),
        "valid_pixels": 0,
        "invalid_source_support": 0,
        "source_window_pixels": 0,
    }
    result = np.full((4, height, width), NODATA, dtype=np.float32)
    if not supported.any():
        return result, counts
    r0 = max(0, math.floor(float(rows[supported].min())))
    c0 = max(0, math.floor(float(cols[supported].min())))
    r1 = min(mapper.full_shape[0], math.floor(float(rows[supported].max())) + 2)
    c1 = min(mapper.full_shape[1], math.floor(float(cols[supported].max())) + 2)
    size = (r1 - r0) * (c1 - c0)
    counts["source_window_pixels"] = size
    if size > maximum_source_pixels:
        raise ValueError("Source window exceeds memory bound; use a smaller output block")
    source_window = (r0, c0, r1 - r0, c1 - c0)
    hh, hv = (reader.read_sigma0(pol, window=source_window) for pol in ("HH", "HV"))
    if not hh["thermal_noise_removed"] or not hv["thermal_noise_removed"]:
        raise ValueError("Corrected product requires actual thermal-noise removal")
    policy = QualityPolicy()
    hh_power, hv_power, incidence = (
        hh["sigma0_linear"],
        hv["sigma0_linear"],
        hh["incidence_angle_deg"],
    )
    valid = (
        np.isfinite(hh_power)
        & (hh_power > 0)
        & np.isfinite(hv_power)
        & (hv_power > 0)
        & np.isfinite(incidence)
        & (incidence >= policy.incidence_min_deg)
        & (incidence <= policy.incidence_max_deg)
    )
    coords = [np.where(supported, rows - r0, 0), np.where(supported, cols - c0, 0)]
    sampled = [
        map_coordinates(
            np.where(valid, values, np.nan),
            coords,
            order=1,
            mode="constant",
            cval=np.nan,
            prefilter=False,
        )
        for values in (hh_power, hv_power, incidence)
    ]
    for values in sampled:
        supported &= np.isfinite(values)
    supported &= (sampled[0] > 0) & (sampled[1] > 0)
    counts["valid_pixels"] = int(supported.sum())
    counts["invalid_source_support"] = counts["geometry_supported"] - counts["valid_pixels"]
    result[0, supported] = 10 * np.log10(sampled[0][supported])
    result[1, supported] = 10 * np.log10(sampled[1][supported])
    result[2, supported] = result[0, supported] - result[1, supported]
    result[3, supported] = sampled[2][supported]
    return result, counts


def inspect_corrected_output(path: Path) -> dict[str, Any]:
    """Inspect every full-resolution pixel and common nodata mask on disk."""
    valid_cog, errors, warnings = cog_validate(str(path), quiet=True)
    reasons = list(errors)
    total = valid = invalid_angles = invalid_ratio = mismatched_masks = nonfinite = 0
    ranges = [[math.inf, -math.inf] for _ in range(4)]
    policy = QualityPolicy()
    with rasterio.open(path) as src:
        if (
            src.count != 4
            or src.dtypes != ("float32",) * 4
            or tuple(src.descriptions) != tuple(BAND_NAMES)
        ):
            raise ValueError("Corrected output must have four explicitly described float32 bands")
        if src.crs != rasterio.crs.CRS.from_epsg(3978) or src.nodata != NODATA:
            reasons.append("Incorrect CRS or nodata")
        if src.transform.a != 40 or src.transform.e != -40 or src.transform.b or src.transform.d:
            reasons.append("Output must use the fixed north-up 40 m grid")
        for _, window in src.block_windows(1):
            values = src.read(window=window)
            masks = values != NODATA
            common = masks.all(axis=0)
            mismatched_masks += int(np.count_nonzero(masks.any(axis=0) != common))
            nonfinite += int(np.count_nonzero(~np.isfinite(values)))
            total += common.size
            valid += int(common.sum())
            if common.any():
                invalid_angles += int(
                    np.count_nonzero(
                        (values[3, common] < policy.incidence_min_deg)
                        | (values[3, common] > policy.incidence_max_deg)
                    )
                )
                invalid_ratio += int(
                    np.count_nonzero(
                        abs(values[2, common] - (values[0, common] - values[1, common])) > 0.001
                    )
                )
                for index in range(4):
                    ranges[index][0] = min(ranges[index][0], float(values[index, common].min()))
                    ranges[index][1] = max(ranges[index][1], float(values[index, common].max()))
        shape, transform, tags = src.shape, tuple(src.transform), src.tags()
        if (
            tags.get("thermal_noise_removed") != "true"
            or tags.get("resampling_domain") != "linear_power"
        ):
            reasons.append("Correction or linear-power provenance missing")
        if tags.get("geolocation_method") != "marine_range_doppler":
            reasons.append("Marine range-Doppler provenance missing")
        if tags.get("fresh_detection_gate") != "blocked":
            reasons.append("Unvalidated detection release must remain blocked")
    if not valid_cog:
        reasons.append("Invalid Cloud Optimized GeoTIFF")
    if not valid or valid / total < policy.min_valid_fraction:
        reasons.append("Insufficient valid output observations")
    for name, count in [
        ("invalid incidence", invalid_angles),
        ("inconsistent ratio", invalid_ratio),
        ("mismatched nodata", mismatched_masks),
        ("nonfinite output", nonfinite),
    ]:
        if count:
            reasons.append(name)
    overview_quality = inspect_overviews(path)
    if not overview_quality["passed"]:
        reasons.append("Stored overview pixels failed semantic checks")
    return {
        "schema_version": 1,
        "overview_quality": overview_quality,
        "scope": "complete_corrected_output",
        "passed": not reasons,
        "reasons": reasons,
        "cog_errors": errors,
        "cog_warnings": warnings,
        "shape": list(shape),
        "transform": list(transform),
        "pixel_spacing_m": 40,
        "total_pixels": total,
        "valid_pixels": valid,
        "valid_fraction": valid / total,
        "invalid_incidence_pixels": invalid_angles,
        "inconsistent_ratio_pixels": invalid_ratio,
        "mismatched_nodata_pixels": mismatched_masks,
        "nonfinite_values": nonfinite,
        "band_ranges": dict(zip(BAND_NAMES, ranges, strict=True)) if valid else {},
        "output_sha256": file_digest(path),
        "policy": policy.model_dump(),
    }


def process_safe_product(
    safe: Path,
    output_dir: Path,
    block_size: int = 256,
    scan_rows: int = 32,
    grid_reference: Path | None = None,
) -> Path:
    """Process the entire product, preserving separate source and output receipts.

    Fresh detection is deliberately gated separately from preprocessing.
    Occupied product destinations are rejected before any input processing.
    """
    if not isinstance(scan_rows, int) or isinstance(scan_rows, bool) or scan_rows < 1:
        raise ValueError("Scan rows must be a positive integer")
    if (
        not isinstance(block_size, int)
        or isinstance(block_size, bool)
        or not 16 <= block_size <= 512
    ):
        raise ValueError("Output block size must be an integer between 16 and 512")
    root = output_dir / safe.stem
    root.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC).isoformat()
    try:
        reader = SAFEProductReader(safe)
        source = safe_source_inventory(reader)
        source_digest = digest(source)
        write_record(
            root / "source-inventory.json", {"product": safe.name, "source_files_sha256": source}
        )
        configuration = {
            "recipe_id": "complete-corrected-safe-v1",
            "policy": QualityPolicy().model_dump(),
            "target_crs": "EPSG:3978",
            "pixel_spacing_m": 40,
            "block_size": block_size,
            "scan_rows": scan_rows,
            "maximum_source_window_pixels": 4_000_000,
            "extent": "all projected annotation knots plus two-pixel margin",
            "reference_grid_sha256": file_digest(grid_reference) if grid_reference else None,
            "grid_phase": "SNAP reference grid phase"
            if grid_reference
            else "national projected origin",
            "resampling": "bilinear point in linear power; positive joint four-neighbor support",
            "scope": "complete source footprint; subsequent target analysis restricted to NL marine AOI",
        }
        write_record(root / "configuration.json", configuration)
        execution, payloads = execution_context()
        with zipfile.ZipFile(root / "executed-code.zip", "x", zipfile.ZIP_DEFLATED) as archive:
            for name, content in payloads.items():
                archive.writestr(name, content)
        write_record(root / "execution-source.json", execution)
        quality = assess_safe_product(reader, root / "source-quality.json", block_rows=scan_rows)
        mapper = MarineGeocoder(reader._find_for_polarisation("HH")[1])
        transform, width, height = (
            product_grid(reader, grid_reference=grid_reference)
            if grid_reference
            else product_grid(reader)
        )
        interim = root / "corrected-interim.tif"
        output = root / (safe.stem + "_4band_EPSG3978.tif")
        temporary = root / "corrected-output.partial.tif"
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 4,
            "dtype": "float32",
            "crs": "EPSG:3978",
            "transform": transform,
            "nodata": NODATA,
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "compress": "DEFLATE",
            "predictor": 3,
            "BIGTIFF": "YES",
        }
        totals = {
            "destination_pixels": 0,
            "geometry_supported": 0,
            "valid_pixels": 0,
            "invalid_source_support": 0,
        }
        maximum_window = 0
        with (
            rasterio.Env(GDAL_CACHEMAX=64 * 1024 * 1024),
            rasterio.open(interim, "w", **profile) as dst,
        ):
            dst.update_tags(
                source_kind="sentinel1_safe",
                processing_quality="passed",
                thermal_noise_removed="true",
                resampling_domain="linear_power",
                fresh_detection_gate="blocked",
                source_inventory_sha256=source_digest,
                source_quality_sha256=file_digest(root / "source-quality.json"),
                configuration_sha256=file_digest(root / "configuration.json"),
                preprocessing_receipt="processing-receipt.json",
                **mapper.provenance(),
            )
            for index, name in enumerate(BAND_NAMES, 1):
                dst.set_band_description(index, name)
            for row in range(0, height, block_size):
                for col in range(0, width, block_size):
                    window = Window(
                        col, row, min(block_size, width - col), min(block_size, height - row)
                    )
                    tile, counts = corrected_tile(reader, mapper, transform, window)
                    dst.write(tile, window=window)
                    for key in totals:
                        totals[key] += counts[key]
                    maximum_window = max(maximum_window, counts["source_window_pixels"])
                logger.info(
                    "Corrected full-product rows %s/%s", min(row + block_size, height), height
                )
        export_complete_cog(interim, temporary)
        output_quality = inspect_corrected_output(temporary)
        write_record(root / "output-quality.json", output_quality)
        if not output_quality["passed"]:
            raise ValueError("Corrected output rejected: " + "; ".join(output_quality["reasons"]))
        if (
            totals["valid_pixels"] != output_quality["valid_pixels"]
            or totals["destination_pixels"] != width * height
        ):
            raise ValueError("Output counts differ from complete processing traversal")
        if safe_source_inventory(reader) != source:
            raise ValueError("Source inputs changed during complete processing")
        if execution_context()[0]["source_files_sha256"] != execution["source_files_sha256"]:
            raise ValueError("Executing source code changed during processing")
        os.replace(temporary, output)
        write_record(
            root / "processing-receipt.json",
            {
                "schema_version": 1,
                "recipe_id": configuration["recipe_id"],
                "product": safe.name,
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat(),
                "source_files_sha256": source,
                "source_inventory_sha256": source_digest,
                "source_quality_sha256": file_digest(root / "source-quality.json"),
                "configuration_sha256": file_digest(root / "configuration.json"),
                "output_quality_sha256": file_digest(root / "output-quality.json"),
                "output": output.name,
                "output_sha256": file_digest(output),
                "output_bytes": output.stat().st_size,
                "source_quality_passed": quality["passed"],
                "output_quality_passed": True,
                "traversal": totals,
                "maximum_source_window_pixels": maximum_window,
                "python_version": platform.python_version(),
                "rasterio_version": rasterio.__version__,
                "execution_source_sha256": file_digest(root / "execution-source.json"),
                "execution_snapshot_sha256": file_digest(root / "executed-code.zip"),
                "reference_comparison_completed": False,
                "fresh_detection_allowed": False,
                "claim_boundary": "Complete corrected product export and structural checks only; independent matched reference validation is required separately.",
            },
        )
        # Only this execution's reproducible temporary intermediate is removed.
        interim.unlink()
        return output
    except (ValueError, OSError, RuntimeError) as error:
        write_record(
            root / "processing-failure.json",
            {
                "product": safe.name,
                "started_at": started,
                "failed_at": datetime.now(UTC).isoformat(),
                "passed": False,
                "reason": str(error),
            },
        )
        raise


def repackage_complete_output(source: Path, destination: Path) -> Path:
    """Preserve prior science and prove lossless replacement packaging pixel by pixel."""
    original_receipt = source.parent / "processing-receipt.json"
    receipt = json.loads(original_receipt.read_text())
    if receipt["output"] != source.name or file_digest(source) != receipt["output_sha256"]:
        raise ValueError("Original complete output receipt changed")
    destination.mkdir(parents=True, exist_ok=False)
    execution, payloads = execution_context()
    write_record(destination / "execution-source.json", execution)
    with zipfile.ZipFile(destination / "executed-code.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, content in payloads.items():
            archive.writestr(name, content)
    output = destination / source.name
    export_complete_cog(source, output)
    compared_pixels = verify_pixel_identity(source, output)
    quality = inspect_corrected_output(output)
    write_record(destination / "output-quality.json", quality)
    if not quality["passed"]:
        raise ValueError("Replacement COG failed complete pixel/overview integrity")
    if execution_context()[0]["source_files_sha256"] != execution["source_files_sha256"]:
        raise ValueError("Repackaging source changed during execution")
    write_record(
        destination / "packaging-receipt.json",
        {
            "schema_version": 1,
            "purpose": "Lossless BigTIFF COG packaging; no recalibration or changed measurements",
            "original_processing_receipt_sha256": file_digest(original_receipt),
            "original_output_sha256": receipt["output_sha256"],
            "output": output.name,
            "output_sha256": file_digest(output),
            "output_bytes": output.stat().st_size,
            "full_resolution_pixels_compared": compared_pixels,
            "all_corrected_pixels_identical": True,
            "output_quality_sha256": file_digest(destination / "output-quality.json"),
            "execution_source_sha256": file_digest(destination / "execution-source.json"),
            "execution_snapshot_sha256": file_digest(destination / "executed-code.zip"),
            "fresh_detection_allowed": False,
        },
    )
    return output


def verify_pixel_identity(source: Path, output: Path) -> int:
    """Compare every base-resolution pixel and its interpreting metadata."""
    compared_pixels = 0
    with (
        rasterio.open(source, OVERVIEW_LEVEL="NONE") as before,
        rasterio.open(output, OVERVIEW_LEVEL="NONE") as after,
    ):
        if (
            before.shape != after.shape
            or before.transform != after.transform
            or before.crs != after.crs
            or before.nodata != after.nodata
            or before.descriptions != after.descriptions
            or before.tags() != after.tags()
        ):
            raise ValueError(
                "Replacement packaging changed raster grid, descriptions or provenance"
            )
        for _, window in before.block_windows(1):
            if not np.array_equal(before.read(window=window), after.read(window=window)):
                raise ValueError("Replacement packaging changed corrected full-resolution pixels")
            compared_pixels += int(window.width * window.height)
    return compared_pixels
