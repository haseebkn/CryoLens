"""Receipt-bound SAFE optical review; sampled locations are never detections."""

from __future__ import annotations

import argparse
import json
import math
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import rasterio
from geoalchemy2.shape import from_shape
from pyproj import Transformer
from rasterio.windows import Window
from shapely.geometry import Point, box, mapping, shape
from shapely.ops import transform as transform_geometry

from cryolens.eval.cohort import acquisition_id, digest, file_digest, load_manifest
from cryolens.geo.aoi import contains_point, load_aoi
from cryolens.ingest.sentinel2 import Sentinel2Client, Sentinel2Provider, utc
from cryolens.ingest.sentinel2_cdse import CDSESentinel2Client
from cryolens.preprocess.masks import LandMaskGenerator
from cryolens.preprocess.product import execution_context, write_record
from cryolens.preprocess.quality import QualityPolicy
from cryolens.preprocess.stack import BAND_NAMES
from cryolens.review.pairs import RadarChip, build_pair, verify_pair_files
from cryolens.review.policy import PairingPolicy


@dataclass
class ReviewLocation:
    id: str
    centroid_wgs84: Any
    scene: Any
    length_m: float | None = None
    width_m: float | None = None


def checked_product(report: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    """Require complete evidence plus the exact raster that passed step 2."""
    if report.get("step2_complete") is not True:
        raise ValueError("Step 2 qualification is incomplete")
    entries = [p for p in report["products"] if p["product"] == name]
    if len(entries) != 1:
        raise ValueError("Product must have one unambiguous qualification")
    p: dict[str, Any] = entries[0]
    policy = QualityPolicy().model_dump()
    if report["policy"] != policy or p["source_quality"]["policy"] != policy:
        raise ValueError("Strict source quality policy changed")
    if not p["representative_preprocessing_passed"] or not p["source_quality"]["passed"]:
        raise ValueError("Source did not pass preprocessing")
    for q in p["source_quality"]["channels"].values():
        if (
            not (0 <= q["nonpositive_fraction"] <= 0.05 and 0.1 <= q["valid_fraction"] <= 1)
            or q["invalid_incidence_pixels"]
        ):
            raise ValueError("Source quality fails fixed limits")
    if len(p["windows"]) != 9:
        raise ValueError("Nine matched reference windows are required")
    for window in p["windows"]:
        checks = window["checks"]
        if not window["scientific_comparison_passed"] or not all(
            c["passed"] for c in checks.values()
        ):
            raise ValueError("Reference comparison failed")
        for band in ("HH", "HV"):
            for stage in ("calibration", "noise"):
                c = checks[stage + "_" + band]
                if not (
                    0 <= c["p95_absolute_error_db"] <= 0.02
                    and 0 <= c["sign_mismatch_fraction"] <= 0.0001
                ):
                    raise ValueError("Radiometric comparison fails fixed limits")
            c = checks["corrected_reprojection_" + band]
            if not (0 <= c["p95_absolute_error_db"] <= 1 and c["matched_pixels"] >= 4096):
                raise ValueError("Corrected comparison fails fixed limits")
            if not 0 <= checks["registration_" + band]["relative_displacement_m"] <= 40:
                raise ValueError("Registration fails fixed limits")
        if not (
            0 <= checks["annotation_geolocation"]["max_error_m"] <= 20
            and 0 <= checks["incidence"]["max_error_deg"] <= 0.05
        ):
            raise ValueError("Geometry comparison fails fixed limits")
    quality = p["output_quality"]
    if not quality["passed"] or not quality["overview_quality"]["passed"]:
        raise ValueError("Complete raster integrity failed")
    checksum = file_digest(path)
    if checksum != p["output_sha256"] or checksum != quality["output_sha256"]:
        raise ValueError("Corrected output differs from its qualification")
    with rasterio.open(path) as src:
        if (
            src.crs != rasterio.crs.CRS.from_epsg(3978)
            or src.count != 4
            or tuple(src.descriptions) != tuple(BAND_NAMES)
        ):
            raise ValueError("Unexpected corrected raster contract")
        if (
            list(src.shape) != quality["shape"]
            or list(src.transform) != quality["transform"]
            or src.transform.a != 40
            or src.transform.e != -40
        ):
            raise ValueError("Corrected grid differs from qualification")
    return p


def radar_window(
    path: Path, row: int, col: int, provenance: dict[str, Any], radius: float = 6000
) -> RadarChip:
    with rasterio.open(path) as src:
        padding = math.ceil(radius / 40) + 2
        window = Window(
            max(0, col - padding),
            max(0, row - padding),
            min(src.width, col + padding + 1) - max(0, col - padding),
            min(src.height, row + padding + 1) - max(0, row - padding),
        )
        arr = src.read([1, 2], window=window, masked=True).astype("float32").filled(np.nan)
        return RadarChip(
            arr[0],
            arr[1],
            [],
            {
                **provenance,
                "review_only": True,
                "native_pixel_spacing_m": 40,
                "native_pixel_window": [
                    int(window.row_off),
                    int(window.col_off),
                    int(window.height),
                    int(window.width),
                ],
                "units": "sigma0_db",
                "native_meaning": "complete corrected 40 m product grid; not original sensor sampling or effective resolution",
            },
            src.window_transform(window),
            "EPSG:3978",
        )


def select_locations(
    path: Path, land: LandMaskGenerator, divisions: int = 5
) -> list[dict[str, Any]]:
    """Predeclared regular spatial lattice; no optical/target-based selection."""
    result = []
    inverse = Transformer.from_crs(3978, 4326, always_xy=True)
    with rasterio.open(path) as src:
        for ri in range(divisions):
            for ci in range(divisions):
                row, col = (
                    int((ri + 0.5) * src.height / divisions),
                    int((ci + 0.5) * src.width / divisions),
                )
                x, y = src.xy(row, col)
                lon, lat = inverse.transform(x, y)
                window = Window(col, row, 1, 1)
                sample = src.read(window=window, masked=True)
                reason = (
                    "outside_nl"
                    if not contains_point(lon, lat)
                    else "invalid_radar"
                    if np.ma.getmaskarray(sample).any() or not np.isfinite(sample.data).all()
                    else "land_or_coastal_buffer"
                    if land.generate_land_mask((1, 1), src.window_transform(window))[0, 0]
                    else None
                )
                result.append(
                    {
                        "row": row,
                        "col": col,
                        "longitude": lon,
                        "latitude": lat,
                        "included": reason is None,
                        "excluded_reason": reason,
                    }
                )
    return result


def select_items(
    search: dict[str, Any], point: Point, acquired: datetime, policy: PairingPolicy
) -> tuple[list[dict[str, Any]], int]:
    projector = Transformer.from_crs(4326, 3978, always_xy=True)
    x, y = projector.transform(point.x, point.y)
    r = policy.maximum_review_radius_m
    view = box(x - r, y - r, x + r, y + r)
    relevant = [
        i
        for i in search["items"]
        if transform_geometry(projector.transform, shape(i["geometry"])).intersects(view)
    ]
    relevant.sort(
        key=lambda i: (
            not shape(i["geometry"]).covers(point),
            abs((utc(i["properties"]["datetime"]) - acquired).total_seconds()),
            i["id"],
        )
    )
    return relevant[: policy.max_items_per_candidate], len(relevant)


def prepare(config_path: Path, output: Path) -> dict[str, Any]:
    """Freeze inputs and sample before either provider is queried."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    report_path = Path(config["validation_report"])
    if file_digest(report_path) != config["validation_report_sha256"]:
        raise ValueError("Qualification report changed")
    policy = PairingPolicy.model_validate(config["pairing_policy"])
    if policy != PairingPolicy():
        raise ValueError("This experiment requires unchanged optical review defaults")
    cohort = load_manifest(Path(config["frozen_manifest"]))
    forbidden = {
        acquisition_id(r["acquisition_id"]) for r in cohort["scenes"] if r["partition"] == "test"
    }
    for entry in config["products"]:
        if acquisition_id(entry["product"]) in forbidden:
            raise ValueError("Held-out acquisitions cannot enter fresh development review")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    write_record(output / "configuration.json", config)
    context, payloads = execution_context()
    with zipfile.ZipFile(output / "executed-code.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, content in payloads.items():
            archive.writestr(name, content)
    write_record(output / "execution.json", context)
    land_path = Path(config["shoreline_cache"])
    if file_digest(land_path) != config["shoreline_sha256"]:
        raise ValueError("Shoreline provenance changed")
    land = LandMaskGenerator(cache_path=land_path)
    selection: dict[str, Any] = {
        "schema_version": 1,
        "selection": "5 by 5 equal-spacing raster lattice; finite NL water centres outside 500 m GSHHG coastal buffer; optical-blind selection",
        "location_kind": "review_location_not_detection",
        "regional_representativeness": False,
        "configuration_sha256": file_digest(output / "configuration.json"),
        "aoi_sha256": digest(load_aoi().__geo_interface__),
        "manifest_sha256": digest(cohort),
        "products": [],
    }
    for entry in config["products"]:
        path = Path(entry["corrected_output"])
        qualified = checked_product(report, entry["product"], path)
        # Acquisition time comes from the authenticated Sentinel-1 product identifier.
        acquired = datetime.strptime(entry["product"].split("_")[4], "%Y%m%dT%H%M%S").replace(
            tzinfo=UTC
        )
        locations = select_locations(path, land)
        with rasterio.open(path) as src:
            projector = Transformer.from_crs(src.crs, 4326, always_xy=True)
            # Densify all bounding edges before projection; rectangle includes nodata.
            b = src.bounds
            coords = (
                [(x, b.bottom) for x in np.linspace(b.left, b.right, 31)]
                + [(b.right, y) for y in np.linspace(b.bottom, b.top, 31)]
                + [(x, b.top) for x in np.linspace(b.right, b.left, 31)]
                + [(b.left, y) for y in np.linspace(b.top, b.bottom, 31)]
            )
            from shapely.geometry import Polygon

            footprint = mapping(
                transform_geometry(projector.transform, Polygon(coords)).intersection(load_aoi())
            )
        selection["products"].append(
            {
                **entry,
                "acquired_utc": acquired.isoformat(),
                "mode": qualified["mode"],
                "output_sha256": qualified["output_sha256"],
                "search_geometry": footprint,
                "search_geometry_scope": "corrected bounding rectangle intersected with NL; includes nodata",
                "locations": locations,
            }
        )
    write_record(output / "selection.json", selection)
    write_record(
        output / "selection-seal.json",
        {
            "selection_sha256": file_digest(output / "selection.json"),
            "configuration_sha256": file_digest(output / "configuration.json"),
            "execution_sha256": file_digest(output / "execution.json"),
            "snapshot_sha256": file_digest(output / "executed-code.zip"),
        },
    )
    return selection


def verify_selection(root: Path) -> None:
    seal = json.loads((root / "selection-seal.json").read_text(encoding="utf-8"))
    for name, key in (
        ("selection.json", "selection_sha256"),
        ("configuration.json", "configuration_sha256"),
        ("execution.json", "execution_sha256"),
        ("executed-code.zip", "snapshot_sha256"),
    ):
        if file_digest(root / name) != seal[key]:
            raise ValueError("Frozen optical sampling record changed: " + name)
    context, _ = execution_context()
    original = json.loads((root / "execution.json").read_text(encoding="utf-8"))
    if context["source_files_sha256"] != original["source_files_sha256"]:
        raise ValueError("Executing source changed since optical preparation; start a new run")


def search_catalogues(root: Path) -> None:
    verify_selection(root)
    selection = json.loads((root / "selection.json").read_text(encoding="utf-8"))
    policy = PairingPolicy.model_validate(
        json.loads((root / "configuration.json").read_text(encoding="utf-8"))["pairing_policy"]
    )
    for p in selection["products"]:
        for client in (Sentinel2Client(), CDSESentinel2Client()):
            dest = root / f"search-{p['mode']}-{client.provider}.json"
            if dest.exists():
                raise FileExistsError("Search receipts are immutable; use a new execution root")
            try:
                result = client.search(
                    p["search_geometry"], utc(p["acquired_utc"]), policy.search_hours
                )
            except Exception as exc:
                result = {
                    "provider": client.provider,
                    "complete": False,
                    "items": [],
                    "failure_type": type(exc).__name__,
                    "reason": "Catalogue request failed; no-coverage inference prohibited",
                    "queried_at": datetime.now(UTC).isoformat(),
                }
            write_record(dest, result)
            print(
                p["mode"],
                client.provider,
                "complete",
                result["complete"],
                "items",
                len(result["items"]),
                flush=True,
            )


def generate(root: Path, provider: str) -> dict[str, Any]:
    verify_selection(root)
    config = json.loads((root / "configuration.json").read_text(encoding="utf-8"))
    selection = json.loads((root / "selection.json").read_text(encoding="utf-8"))
    if file_digest(root / "configuration.json") != selection["configuration_sha256"]:
        raise ValueError("Run configuration changed")
    report_path = Path(config["validation_report"])
    if file_digest(report_path) != config["validation_report_sha256"]:
        raise ValueError("Qualification report changed")
    qualification = json.loads(report_path.read_text(encoding="utf-8"))
    policy = PairingPolicy.model_validate(config["pairing_policy"])
    # Reserve each execution: interruption never silently overwrites evidence.
    run = root / provider
    run.mkdir(exist_ok=False)
    client: Sentinel2Provider = (
        Sentinel2Client()
        if provider == "planetary-computer"
        else CDSESentinel2Client(cache=Path(config["cdse_cache"]))
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "provider": provider,
        "started_at": datetime.now(UTC).isoformat(),
        "selection_sha256": file_digest(root / "selection.json"),
        "denominator": "predeclared valid NL marine review locations, not target candidates or regional area",
        "locations": [],
        "detector_executed": False,
        "human_assessments_created": 0,
        "fresh_detection_gate_open": False,
    }
    for product in selection["products"]:
        path = Path(product["corrected_output"])
        checked_product(qualification, product["product"], path)
        search_path = root / f"search-{product['mode']}-{provider}.json"
        search = json.loads(search_path.read_text(encoding="utf-8"))
        acquired = utc(product["acquired_utc"])
        for location in product["locations"]:
            if not location["included"]:
                continue
            point = Point(location["longitude"], location["latitude"])
            identifier = digest(
                {
                    "source": product["output_sha256"],
                    "row": location["row"],
                    "col": location["col"],
                    "selection": result["selection_sha256"],
                }
            )
            subject = ReviewLocation(
                identifier,
                from_shape(point, srid=4326),
                SimpleNamespace(acquisition_time=acquired, product_id=product["product"]),
            )
            radar = radar_window(
                path,
                location["row"],
                location["col"],
                {
                    "source_product_id": product["product"],
                    "source_sha256": product["output_sha256"],
                    "qualification_sha256": config["validation_report_sha256"],
                    "mode": product["mode"],
                },
            )
            selected, total = select_items(search, point, acquired, policy)
            pairs, failures = [], []
            for item in selected:
                try:
                    pair = build_pair(subject, radar, item, client, run / "pairs", policy, search)
                    verify_pair_files(run / "pairs" / pair["id"], pair)
                    pairs.append(pair)
                except Exception as exc:
                    failures.append(
                        {
                            "item_id": item["id"],
                            "failure_type": type(exc).__name__,
                            "reason": "Optical read or processing failed; not no coverage",
                        }
                    )
            if not pairs:
                reason = (
                    "Optical asset read failed"
                    if failures
                    else "Catalogue search failed"
                    if not search["complete"]
                    else "No overlapping optical tile in the fixed temporal window"
                )
                pairs.append(
                    build_pair(subject, radar, None, client, run / "pairs", policy, search, reason)
                )
            record = {
                "id": identifier,
                "mode": product["mode"],
                "product": product["product"],
                "longitude": point.x,
                "latitude": point.y,
                "search_complete": search["complete"],
                "search_sha256": file_digest(search_path),
                "relevant_items": total,
                "selected_items": len(selected),
                "selection_truncated": total > len(selected),
                "asset_failures": failures,
                "pairs": [
                    {
                        "id": p["id"],
                        "manifest_sha256": file_digest(run / "pairs" / p["id"] / "pair.json"),
                        "optical_item_id": p["optical_item_id"],
                        "visibility": p["visibility"],
                        "full_movement_envelope_in_chip": p["full_movement_envelope_in_chip"],
                    }
                    for p in pairs
                ],
            }
            result["locations"].append(record)
            write_record(run / (identifier + "-receipt.json"), record)
            print(
                product["mode"],
                len(result["locations"]),
                [p["visibility"]["status"] for p in pairs],
                flush=True,
            )
        if file_digest(path) != product["output_sha256"]:
            raise ValueError("Corrected raster changed during pairing")
    rows = result["locations"]
    result["summary"] = {
        "review_locations": len(rows),
        "with_downloaded_optical_context": sum(
            any(p["optical_item_id"] for p in r["pairs"]) for r in rows
        ),
        "screened_useful": sum(
            any(p["visibility"]["useful_for_review"] for p in r["pairs"]) for r in rows
        ),
        "screened_useful_with_full_envelope": sum(
            any(
                p["visibility"]["useful_for_review"] and p["full_movement_envelope_in_chip"]
                for p in r["pairs"]
            )
            for r in rows
        ),
        "locations_with_asset_failure": sum(bool(r["asset_failures"]) for r in rows),
        "locations_with_incomplete_search": sum(not r["search_complete"] for r in rows),
        "locations_with_truncated_selection": sum(r["selection_truncated"] for r in rows),
    }
    result["completed_at"] = datetime.now(UTC).isoformat()
    result["claim_boundary"] = (
        "Screening is not analyst inspection, optical corroboration, target accuracy or an NL-wide availability estimate. Missing counterparts never imply false SAR targets."
    )
    verify_selection(root)
    write_record(run / "report.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "search", "generate", "serve"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--provider", choices=["planetary-computer", "cdse"], default="planetary-computer"
    )
    parser.add_argument("--port", type=int, default=8019)
    args = parser.parse_args()
    if args.stage == "prepare":
        if args.config is None:
            parser.error("--config required for preparation")
        prepare(args.config, args.root)
    elif args.stage == "search":
        search_catalogues(args.root)
    elif args.stage == "generate":
        generate(args.root, args.provider)
    else:
        import uvicorn

        from cryolens.review.fresh_app import create_app

        uvicorn.run(create_app(args.root / args.provider), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
