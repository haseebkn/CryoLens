"""Receipt-bound NL chart-proxy chips, selected only from frozen development data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
from netCDF4 import Dataset
from numpy.typing import NDArray
from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import shape
from shapely.ops import transform, unary_union

from cryolens.data.ai4arctic import PUBLISHER_MEAN_STD
from cryolens.eval.cohort import (
    digest,
    file_digest,
    load_manifest,
    select_partition,
    verify_sources,
)
from cryolens.eval.tracking import code_provenance

IGNORE = 255


def chart_labels(sic: NDArray[Any], eligible: NDArray[Any]) -> NDArray[np.uint8]:
    """Keep unknown charts excluded; SIC 0-1 water, SIC 2-10 ice affected."""
    result = np.full(sic.shape, IGNORE, dtype=np.uint8)
    valid = eligible & np.isfinite(sic) & (sic >= 0) & (sic <= 10) & (sic == np.floor(sic))
    result[valid] = (sic[valid] >= 2).astype(np.uint8)
    return result


def development_records(manifest: dict[str, Any], partition: str) -> list[Any]:
    """Reject test before source resolution or array access, with no release escape."""
    if partition not in {"train", "validation"}:
        raise ValueError("Segmentation experiments accept development partitions only")
    return select_partition(manifest, partition, "sea_ice_segmentation")  # type: ignore[arg-type]


def read_slice(ds: Any, name: str, window: tuple[slice, slice]) -> NDArray[np.float32]:
    var = ds[name]
    raw = np.ma.filled(var[window].astype(np.float32), np.nan)
    fill = getattr(var, "variable_fill_value", None)
    if fill is not None:
        raw[raw == float(fill)] = np.nan
    return np.asarray(raw, dtype=np.float32)


def tie_window(
    grid: NDArray[Any], full: tuple[int, int], row: int, col: int, size: int
) -> NDArray[np.float64]:
    """Apply the existing endpoint tie-grid convention without allocating a swath."""
    rr = np.arange(row, row + size) * (grid.shape[0] - 1) / (full[0] - 1)
    cc = np.arange(col, col + size) * (grid.shape[1] - 1) / (full[1] - 1)
    r0 = np.minimum(rr.astype(int), grid.shape[0] - 2)
    c0 = np.minimum(cc.astype(int), grid.shape[1] - 2)
    wr, wc = (rr - r0)[:, None], (cc - c0)[None, :]
    top = grid[r0][:, c0] * (1 - wc) + grid[r0][:, c0 + 1] * wc
    bottom = grid[r0 + 1][:, c0] * (1 - wc) + grid[r0 + 1][:, c0 + 1] * wc
    return np.asarray(top * (1 - wr) + bottom * wr, dtype=np.float64)


def normalize_db(inputs: NDArray[Any]) -> NDArray[np.float32]:
    """Same declared physical-dB adapter for both models, no scene/test fitting."""
    means = np.asarray([-20.0, -30.0], dtype=np.float32)[None, :, None, None]
    return np.asarray((np.clip(inputs, -60, 10) - means) / 10.0, dtype=np.float32)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def prepare(config: dict[str, Any], project: Path, output: Path) -> dict[str, Any]:
    """Seal configuration before reading development labels; never open test sources."""
    import zipfile

    import geopandas as gpd

    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "configuration.json", config)
    manifest_path = project / config["manifest"]
    manifest = load_manifest(manifest_path)
    data_root = Path(config["data_root"])
    shoreline = Path(config["shoreline_cache"])
    if file_digest(shoreline) != config["shoreline_sha256"]:
        raise ValueError("Shoreline differs from predeclared source")
    coast = gpd.read_file(shoreline).to_crs("EPSG:3978")
    land = unary_union(coast.geometry).buffer(config["coastal_buffer_m"])
    if land.is_empty or not land.is_valid:
        raise ValueError("Invalid shoreline; no all-water fallback")
    to_project = Transformer.from_crs("EPSG:4326", "EPSG:3978", always_xy=True).transform
    aoi = transform(to_project, shape(manifest["aoi"]["geometry"]))
    provenance = code_provenance(project)
    write_json(output / "executed-code.json", provenance)
    with zipfile.ZipFile(output / "executed-code.zip", "x", zipfile.ZIP_DEFLATED) as z:
        for name, sha in provenance["source_files_sha256"].items():
            f = project / name
            if file_digest(f) != sha:
                raise ValueError("Source changed during snapshot")
            z.write(f, name)
    result: dict[str, Any] = {
        "schema_version": 1,
        "task": "sea_ice_segmentation",
        "test_access": False,
        "manifest_sha256": digest(manifest),
        "configuration_sha256": digest(config),
        "code_sha256": provenance["code_sha256"],
        "labels": ["open_water", "ice_affected"],
        "ignore_index": IGNORE,
        "partitions": {},
        "claim_boundary": "Sampled development chips; chart agreement, not field truth or target accuracy",
    }
    size = int(config["patch_size"])
    for partition in ["train", "validation"]:
        records = development_records(manifest, partition)
        verify_sources(records, data_root)
        patches: list[NDArray[Any]] = []
        labels: list[NDArray[Any]] = []
        positions: list[dict[str, Any]] = []
        scene_results: list[dict[str, Any]] = []
        for record in records:
            path = (data_root / record.source_path).resolve()
            if not path.is_relative_to(data_root.resolve()):
                raise ValueError("Dataset source escaped root")
            scene_count = 0
            with Dataset(path) as ds:
                for name, polarization in [
                    ("nersc_sar_primary", "HH"),
                    ("nersc_sar_secondary", "HV"),
                ]:
                    if getattr(ds[name], "polarisation", None) != polarization:
                        raise ValueError("Expected HH/HV channel order")
                if float(getattr(ds, "pixel_spacing", 80)) != 80:
                    raise ValueError("This experiment requires the established 80 m grid")
                full = ds["nersc_sar_primary"].shape
                if any(
                    ds[n].shape != full
                    for n in ["nersc_sar_secondary", "SIC", "distance_map", "sar_incidenceangle"]
                ):
                    raise ValueError("Source arrays are not aligned")
                latg = np.asarray(ds["sar_grid2d_latitude"][:])
                long = np.asarray(ds["sar_grid2d_longitude"][:])
                if not np.isfinite(latg).all() or not np.isfinite(long).all():
                    raise ValueError("Nonfinite geolocation")
                for name in [
                    "nersc_sar_primary",
                    "nersc_sar_secondary",
                    "SIC",
                    "distance_map",
                    "sar_incidenceangle",
                ]:
                    ds[name].set_var_chunk_cache(size=64 * 1024**2, nelems=1009, preemption=0.75)
                coords = [
                    (r, col)
                    for r in range(0, full[0] - size + 1, size)
                    for col in range(0, full[1] - size + 1, size)
                ]
                coords.sort(key=lambda rc: digest([config["seed"], record.source_sha256, list(rc)]))
                considered = 0
                for row, col in coords:
                    considered += 1
                    lat = tie_window(latg, full, row, col, size)
                    lon = tie_window(long, full, row, col, size)
                    xx, yy = to_project(lon, lat)
                    geographic = contains_xy(aoi, xx, yy) & ~contains_xy(land, xx, yy)
                    if geographic.mean() < config["minimum_scored_fraction"]:
                        continue
                    window = (slice(row, row + size), slice(col, col + size))
                    distance = read_slice(ds, "distance_map", window)
                    dm, sd = PUBLISHER_MEAN_STD["distance_map"]
                    distance = distance * sd + dm
                    inc = read_slice(ds, "sar_incidenceangle", window)
                    im, iss = PUBLISHER_MEAN_STD["sar_incidenceangle"]
                    inc = inc * iss + im
                    sic = read_slice(ds, "SIC", window)
                    inputs = []
                    for name in ["nersc_sar_primary", "nersc_sar_secondary"]:
                        mean, std = PUBLISHER_MEAN_STD[name]
                        inputs.append(read_slice(ds, name, window) * std + mean)
                    x = np.stack(inputs)
                    eligible = (
                        geographic
                        & np.isfinite(x).all(axis=0)
                        & np.isfinite(distance)
                        & (distance >= 1)
                        & (distance <= 41.01)
                        & (inc >= 15)
                        & (inc <= 50)
                    )
                    y = chart_labels(sic, eligible)
                    if (y != IGNORE).mean() < config["minimum_scored_fraction"]:
                        continue
                    # Model context outside the eligible region is neutralized identically.
                    for channel, neutral in enumerate([-20, -30]):
                        x[channel, ~eligible] = neutral
                    patches.append(x.astype(np.float32))
                    labels.append(y)
                    positions.append(
                        {
                            "scene_id": record.scene_id,
                            "group_id": record.group_id,
                            "source_sha256": record.source_sha256,
                            "row": row,
                            "col": col,
                        }
                    )
                    scene_count += 1
                    if scene_count >= config["patches_per_scene"][partition]:
                        break
                scene_results.append(
                    {
                        "scene_id": record.scene_id,
                        "group_id": record.group_id,
                        "selected_patches": scene_count,
                        "considered_grid_positions": considered,
                        "exclusion_reason": None
                        if scene_count
                        else "No chip met the predeclared valid NL/chart support",
                    }
                )
            if file_digest(path) != record.source_sha256:
                raise ValueError("Source changed during chip generation")
            print(partition, record.scene_id, "patches", scene_count, flush=True)
        if not patches:
            raise ValueError(f"No valid {partition} training/evaluation support")
        xall, yall = np.stack(patches), np.stack(labels)
        target = output / f"{partition}.npz"
        np.savez_compressed(target, inputs_db=xall, labels=yall)
        result["partitions"][partition] = {
            "file": target.name,
            "sha256": file_digest(target),
            "patches": len(patches),
            "scored_pixels": int((yall != IGNORE).sum()),
            "class_counts": [int((yall == k).sum()) for k in [0, 1]],
            "positions": positions,
            "scenes": scene_results,
        }
    if code_provenance(project)["code_sha256"] != provenance["code_sha256"]:
        raise ValueError("Source changed during preparation; keep failure and rerun in a new root")
    write_json(output / "dataset.json", result)
    return result
