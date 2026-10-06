"""Build real SAR/optical review artifacts, with native pixels and explicit uncertainty."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import rasterio
from geoalchemy2.shape import to_shape
from pyproj import Transformer
from rasterio.control import GroundControlPoint
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject
from scipy.ndimage import binary_dilation

from cryolens.data.ai4arctic import AI4ArcticScene, load_scene
from cryolens.db.models import DetectionModel
from cryolens.eval.cohort import acquisition_id, digest, file_digest, load_manifest
from cryolens.geo.aoi import contains_point, load_aoi, raster_aoi_mask
from cryolens.ingest.sentinel2 import Sentinel2Client, utc
from cryolens.review.policy import PairingPolicy

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


@dataclass
class RadarChip:
    hh: np.ndarray
    hv: np.ndarray
    gcps: list[GroundControlPoint]
    source: dict[str, Any]


@lru_cache(maxsize=1)
def _scene(path: str, file_signature: tuple[int, int]) -> tuple[AI4ArcticScene, str]:
    return load_scene(path, load_context=False), file_digest(Path(path))


def radar_chip(detection: DetectionModel, data_root: Path, radius_m: float) -> RadarChip:
    """Use established AI4Arctic native pixels; fresh SAFE deployment remains blocked."""
    metadata = detection.scene.processing_provenance
    if metadata.get("preprocessing") != "publisher_ready_to_train":
        raise ValueError("Paired review currently requires established AI4Arctic radar processing")
    original = metadata["source_product_id"]
    manifest = load_manifest(Path("docs/evaluation/v1/manifest.json"))
    records = [r for r in manifest["scenes"] if r["acquisition_id"] == acquisition_id(original)]
    if not records or any(r["partition"] == "test" for r in records):
        raise ValueError("Radar input must be a frozen development acquisition")
    filename = metadata["source_filename"]
    if Path(filename).name != filename:
        raise ValueError("Radar source filename must be a basename")
    paths = [data_root / r["source_path"] for r in records]
    paths += [data_root / alias for r in records for alias in r.get("source_aliases", [])]
    path = next((p for p in paths if p.is_file() and p.name == filename), None)
    if path is None:
        raise FileNotFoundError("Verified native radar source is unavailable")
    stat = path.stat()
    scene, checksum = _scene(str(path.resolve()), (stat.st_size, stat.st_mtime_ns))
    if checksum != metadata["source_sha256"] or checksum not in {
        r["source_sha256"] for r in records
    }:
        raise ValueError("Radar source changed since evaluation/import")
    r0, c0, r1, c1 = detection.detector_params["pixel_bbox"]
    center_r, center_c = (r0 + r1) // 2, (c0 + c1) // 2
    padding = math.ceil(radius_m / scene.pixel_spacing_m) + 10
    r0, c0 = max(0, center_r - padding), max(0, center_c - padding)
    r1, c1 = (
        min(scene.shape[0], center_r + padding + 1),
        min(scene.shape[1], center_c + padding + 1),
    )
    rows, cols = np.linspace(r0, r1 - 1, 9, dtype=int), np.linspace(c0, c1 - 1, 9, dtype=int)
    gcps = [
        GroundControlPoint(
            row=float(r - r0) + 0.5,
            col=float(c - c0) + 0.5,
            x=float(scene.longitude[r, c]),
            y=float(scene.latitude[r, c]),
        )
        for r in rows
        for c in cols
    ]
    return RadarChip(
        scene.sigma0_hh_db[r0:r1, c0:c1],
        scene.sigma0_hv_db[r0:r1, c0:c1],
        gcps,
        {
            "source_product_id": original,
            "source_filename": filename,
            "source_sha256": checksum,
            "native_pixel_window": [r0, c0, r1, c1],
            "native_pixel_spacing_m": scene.pixel_spacing_m,
            "units": "sigma0_db",
            "normalization": scene.assumptions["normalisation"],
            "geolocation": "publisher tie-point grid; interpolated annotation GCPs, not surveyed absolute accuracy",
        },
    )


def assess_visibility(
    scl: np.ndarray, rgb_valid: np.ndarray, domain: np.ndarray, policy: PairingPolicy
) -> dict[str, Any]:
    """SCL is screening evidence, not a pixel-level guarantee of clear sky."""
    denominator = int(domain.sum())
    if not denominator:
        raise ValueError("Visibility needs a nonempty inspected region")
    invalid = ~rgb_valid | np.isin(scl, [0, 1])
    clouds = np.isin(scl, [3, 8, 9, 10])
    buffer_px = math.ceil(policy.cloud_buffer_m / policy.review_grid_spacing_m)
    if buffer_px:
        yy, xx = np.ogrid[-buffer_px : buffer_px + 1, -buffer_px : buffer_px + 1]
        clouds = binary_dilation(clouds, structure=(xx * xx + yy * yy <= buffer_px * buffer_px))
    ambiguous = np.isin(scl, [2, 7])
    visible = ~invalid & ~clouds & ~ambiguous & np.isin(scl, [4, 5, 6, 11])
    fraction = float(np.count_nonzero(visible & domain) / denominator)
    valid_fraction = float(np.count_nonzero(~invalid & domain) / denominator)
    return {
        "status": "screened_useful"
        if fraction >= policy.useful_visible_fraction
        else "limited_visibility"
        if valid_fraction
        else "unavailable",
        "useful_for_review": fraction >= policy.useful_visible_fraction,
        "visible_fraction": fraction,
        "valid_fraction": valid_fraction,
        "cloud_or_shadow_buffered_fraction": float(np.count_nonzero(clouds & domain) / denominator),
        "ambiguous_scl_fraction": float(np.count_nonzero(ambiguous & domain) / denominator),
        "snow_ice_fraction": float(np.count_nonzero((scl == 11) & domain) / denominator),
        "inspected_pixels": denominator,
        "quality_native_spacing_m": 20,
        "analyst_inspection_required": True,
        "interpretation": "SCL screening only; cloud/ice confusion, haze and subpixel targets remain possible",
    }


def _warp(
    path: Path, transform: Any, size: int, categorical: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    output = np.zeros((size, size), dtype=np.float32)
    mask = np.zeros((size, size), dtype=np.uint8)
    with rasterio.open(path) as src:
        reproject(
            rasterio.band(src, 1),
            output,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=transform,
            dst_crs="EPSG:3978",
            dst_nodata=0,
            resampling=Resampling.nearest if categorical else Resampling.bilinear,
        )
        reproject(
            src.read_masks(1),
            mask,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs="EPSG:3978",
            resampling=Resampling.nearest,
        )
    valid = (mask > 0) & np.isfinite(output) & (output > 0)
    output[~valid] = 0
    return output, valid


def build_pair(
    detection: DetectionModel,
    radar: RadarChip,
    item: dict[str, Any] | None,
    client: Sentinel2Client,
    root: Path,
    policy: PairingPolicy,
    search_receipt: dict[str, Any],
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    point = to_shape(detection.centroid_wgs84)
    if not contains_point(point.x, point.y):
        raise ValueError("Candidate outside the NL study polygon")
    sar_time = utc(detection.scene.acquisition_time)
    optical_time = utc(item["properties"]["datetime"]) if item else None
    seconds = (optical_time - sar_time).total_seconds() if optical_time else None
    if seconds is not None and abs(seconds) > policy.search_hours * 3600:
        raise ValueError("Optical acquisition exceeds the permitted temporal window")
    match_radius = policy.matching_radius(
        seconds or 0, max(detection.length_m or 0, detection.width_m or 0)
    )
    radius = min(policy.maximum_review_radius_m, max(policy.minimum_review_radius_m, match_radius))
    x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(point.x, point.y)
    size = math.ceil(2 * radius / policy.review_grid_spacing_m)
    radius = size * policy.review_grid_spacing_m / 2
    transform = from_origin(
        x - radius, y + radius, policy.review_grid_spacing_m, policy.review_grid_spacing_m
    )
    code_hashes = {
        "pairs.py": file_digest(Path(__file__)),
        "policy.py": file_digest(Path(__file__).with_name("policy.py")),
        "sentinel2.py": file_digest(Path(__file__).parents[1] / "ingest/sentinel2.py"),
        "ai4arctic.py": file_digest(Path(__file__).parents[1] / "data/ai4arctic.py"),
    }
    aoi_hash = digest(load_aoi().__geo_interface__)
    pair_id = digest(
        {
            "detection": detection.id,
            "radar": radar.source,
            "optical": item,
            "policy": policy.model_dump(),
            "generation_code_sha256": code_hashes,
            "aoi_sha256": aoi_hash,
            "unavailable_reason": unavailable_reason if item is None else None,
        }
    )
    output = root / pair_id
    if (output / "pair.json").is_file():
        existing: dict[str, Any] = json.loads((output / "pair.json").read_text())
        verify_pair_files(output, existing)
        return existing
    output.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        output / "radar-native.tif",
        "w",
        driver="GTiff",
        width=radar.hv.shape[1],
        height=radar.hv.shape[0],
        count=2,
        dtype="float32",
        nodata=np.nan,
        compress="deflate",
    ) as dst:
        dst.gcps = radar.gcps, rasterio.crs.CRS.from_epsg(4326)
        dst.write(radar.hh.astype(np.float32), 1)
        dst.write(radar.hv.astype(np.float32), 2)
        dst.set_band_description(1, "sigma0_hh_db")
        dst.set_band_description(2, "sigma0_hv_db")
    radar_aligned = np.full((size, size), np.nan, dtype=np.float32)
    reproject(
        radar.hv,
        radar_aligned,
        gcps=radar.gcps,
        src_crs="EPSG:4326",
        src_nodata=np.nan,
        dst_transform=transform,
        dst_crs="EPSG:3978",
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    rgb = None
    visibility: dict[str, Any] = {
        "status": "unavailable",
        "useful_for_review": False,
        "reason": unavailable_reason or "No optical acquisition within search window",
    }
    assets = {}
    if item:
        assets = client.download_windows(
            item, (x - radius, y - radius, x + radius, y + radius), output
        )
        arrays, masks = {}, {}
        for band in ("B04", "B03", "B02", "B08", "SCL"):
            arrays[band], masks[band] = _warp(
                output / f"{band}-native.tif", transform, size, band == "SCL"
            )
        common_valid = masks["B04"] & masks["B03"] & masks["B02"] & masks["SCL"]
        columns, rows = np.meshgrid(np.arange(size) + 0.5, np.arange(size) + 0.5)
        domain = (
            (columns - size / 2) ** 2 + (rows - size / 2) ** 2
        ) * policy.review_grid_spacing_m**2 <= min(radius, match_radius) ** 2
        domain &= raster_aoi_mask((size, size), transform, "EPSG:3978")
        visibility = assess_visibility(arrays["SCL"], common_valid, domain, policy)
        # A fixed DN display stretch is never presented as quantitative reflectance.
        rgb = np.clip(np.stack([arrays[b] for b in ("B04", "B03", "B02")], axis=-1) / 3000, 0, 1)
        rgb[~common_valid] = 0.15  # Grey is explicitly labelled missing data, not dark ocean.
        aligned = output / "optical-aligned.tif"
        with rasterio.open(
            aligned,
            "w",
            driver="GTiff",
            width=size,
            height=size,
            count=5,
            dtype="float32",
            crs="EPSG:3978",
            transform=transform,
            nodata=0,
            compress="deflate",
        ) as dst:
            for index, band in enumerate(("B04", "B03", "B02", "B08", "SCL"), 1):
                dst.write(arrays[band], index)
                dst.set_band_description(index, band)
            dst.write_mask(common_valid.astype(np.uint8) * 255)
    figure, axes = plt.subplots(1, 2, figsize=(11, 5), constrained_layout=True)
    extent = (-radius / 1000, radius / 1000, -radius / 1000, radius / 1000)
    axes[0].imshow(
        radar_aligned, extent=extent, cmap="gray", vmin=-35, vmax=-10, interpolation="nearest"
    )
    axes[0].set_title("Sentinel-1 HV σ⁰ dB\n" + sar_time.isoformat())
    if rgb is not None:
        axes[1].imshow(rgb, extent=extent, interpolation="nearest")
        axes[1].set_title(
            "Sentinel-2 RGB (DN stretch; grey = no data)\n"
            + str(optical_time.isoformat() if optical_time else "")
        )
    else:
        axes[1].text(
            0.5,
            0.5,
            visibility["reason"],
            ha="center",
            va="center",
            wrap=True,
            transform=axes[1].transAxes,
        )
        axes[1].set_xlim(extent[:2])
        axes[1].set_ylim(extent[2:])
    for axis in axes:
        axis.plot(0, 0, "r+", markersize=10)
        axis.add_patch(
            plt.Circle((0, 0), match_radius / 1000, fill=False, color="#e6b755", linestyle="--")
        )
        axis.set_xlim(extent[:2])
        axis.set_ylim(extent[2:])
        axis.set_xlabel("Projected distance east from radar position (km)")
        axis.set_ylabel("Projected distance north (km)")
    separation = f"Δt={seconds / 3600:+.2f} h" if seconds is not None else "Optical unavailable"
    envelope_note = "; envelope extends beyond chip" if match_radius > radius else ""
    figure.suptitle(
        f"{separation} · {visibility['status']} · radar {radar.source['native_pixel_spacing_m']:.0f} m samples / optical 10 m / SCL 20 m\nAssumed movement+geolocation radius {match_radius / 1000:.2f} km{envelope_note}. Missing counterpart never implies false radar detection",
        fontsize=10,
    )
    figure.savefig(output / "paired.png", dpi=160)
    plt.close(figure)
    manifest = {
        "schema_version": 1,
        "generation_code_sha256": code_hashes,
        "aoi_sha256": aoi_hash,
        "id": pair_id,
        "detection_id": detection.id,
        "sar_product_id": detection.scene.product_id,
        "sar_acquired_utc": sar_time.isoformat(),
        "optical_item_id": item["id"] if item else None,
        "optical_acquired_utc": optical_time.isoformat() if optical_time else None,
        "signed_time_separation_seconds": seconds,
        "candidate_lonlat": [point.x, point.y],
        "policy": policy.model_dump(),
        "matching_radius_m": match_radius,
        "review_radius_m": radius,
        "full_movement_envelope_in_chip": match_radius <= radius,
        "movement_assumption_is_not_a_drift_forecast": True,
        "radar": radar.source,
        "optical_assets": assets,
        "visibility": visibility,
        "search": search_receipt,
        "alignment": {
            "crs": "EPSG:3978",
            "transform": list(transform),
            "shape": [size, size],
            "display_spacing_m": policy.review_grid_spacing_m,
            "rgb_resampling": "bilinear DN",
            "radar_resampling": "nearest dB, display only; no new SAR resolution",
            "quality_resampling": "nearest; 20 m source preserved",
            "valid_data": "nearest source masks intersected across RGB and SCL; invalid display pixels zeroed",
        },
        "generated_at": datetime.now(UTC).isoformat(),
        "automated_target_verdict": None,
        "files": {
            p.name: file_digest(p)
            for p in sorted(output.iterdir())
            if p.is_file() and p.name != "pair.json" and not p.name.endswith(".part")
        },
    }
    temporary = output / "pair.json.part"
    temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(output / "pair.json")
    return manifest


def verify_pair_files(directory: Path, manifest: dict[str, Any]) -> None:
    for name, expected in manifest["files"].items():
        if (
            Path(name).name != name
            or not (directory / name).is_file()
            or file_digest(directory / name) != expected
        ):
            raise ValueError("Paired review artifact missing or changed: " + name)
