"""Publish source-bound acquisition-search outcomes without raw rasters or secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import shape

from cryolens.eval.cohort import file_digest
from cryolens.geo.aoi import load_aoi
from cryolens.preprocess.provenance import safe_file_inventory
from cryolens.preprocess.quality import QualityPolicy
from cryolens.preprocess.safe_reader import _open_path


def publication_quality(path: Path, require_binding: bool = False) -> dict[str, Any]:
    quality: dict[str, Any] = json.loads(path.read_text())
    policy = QualityPolicy().model_dump()
    if quality.get("scope") != "full_measurement_grid" or quality.get("policy") != policy:
        raise ValueError("Unqualified quality-report scope or changed thresholds")
    binding_path = path.with_suffix(".binding.json")
    if binding_path.is_file():
        binding = json.loads(binding_path.read_text())
        if (
            binding.get("report_sha256") != file_digest(path)
            or binding.get("inputs_and_code", {}).get("policy") != policy
        ):
            raise ValueError("Quality binding report digest or policy changed")
    elif require_binding:
        raise ValueError("New scan lacks a strict source-bound execution")
    return quality


def selected_records(plans: list[Path]) -> dict[str, dict[str, Any]]:
    """Scope a publication to declared plans, rejecting conflicting identities."""
    selected: dict[str, dict[str, Any]] = {}
    for path in plans:
        for row in json.loads(path.read_text())["selected"]:
            prior = selected.get(row["name"])
            if prior is not None and prior["id"] != row["id"]:
                raise ValueError("Conflicting catalogue identities in acquisition plans")
            selected.setdefault(row["name"], row)
    return selected


def plan_output_names(plans: list[Path]) -> list[str]:
    names = [path.name for path in plans]
    if len(names) == len({name.casefold() for name in names}):
        return names
    return [f"{index:02d}-{path.name}" for index, path in enumerate(plans, 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--plan",
        type=Path,
        action="append",
        default=[],
        help="Explicit acquisition plan(s); omit only for the historical default plan directory",
    )
    parser.add_argument(
        "--quality-dir",
        type=Path,
        action="append",
        default=[],
        help="Explicit newer source-bound scans; original evidence remains preserved",
    )
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve existing evidence; choose a new output")
    plans = args.plan or sorted(Path("data/processed").glob("acquisition-plan*.json"))
    if not plans:
        raise ValueError("No acquisition plans supplied")
    selected = selected_records(plans)
    plan_names = plan_output_names(plans)
    quality_paths = list((args.root / "quality").glob("*.json")) + list(
        args.root.glob("validation-*/*full-quality.json")
    )
    quality_by_name: dict[str, tuple[Path, dict[str, Any]]] = {}
    for p in quality_paths:
        if p.name.endswith(".binding.json"):
            continue
        q = publication_quality(p)
        name = q["product"]
        if name in quality_by_name and quality_by_name[name][1]["channels"] != q["channels"]:
            raise ValueError("Independent scans disagree")
        quality_by_name[name] = (p, q)
    for directory in args.quality_dir:
        for path in directory.glob("*.json"):
            if path.name.endswith(".binding.json"):
                continue
            q = publication_quality(path, require_binding=True)
            quality_by_name[q["product"]] = (path, q)
    rows = []
    for name, r in selected.items():
        product = args.root / "raw" / name.removesuffix(".SAFE")
        receipt_path = product / "download-receipt.json"
        row = {
            "product": name,
            "catalogue_id": r["id"],
            "mode": r["mode"],
            "acquired_utc": r["date"],
            "footprint": r["catalogue"]["GeoFootprint"],
            "approximate_aoi_fraction_from_lonlat_footprint": r["aoi_fraction"],
            "purpose": "processing development only; no frozen test access",
            "comparison_status": "not_qualified",
        }
        if not receipt_path.is_file():
            row["status"] = "metadata_excluded_without_full_download"
            row["reason"] = (
                "Nonzero reference height is outside currently qualified geometry convention"
            )
            rows.append(row)
            continue
        receipt = json.loads(receipt_path.read_text())
        catalogue_url = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products({r['id']})"
        response = httpx.get(catalogue_url, timeout=90)
        response.raise_for_status()
        live_catalogue = response.json()
        if any(
            live_catalogue[key] != receipt["catalogue"][key]
            for key in ("Id", "Name", "ContentLength")
        ):
            raise ValueError("Current CDSE catalogue identity differs from download receipt")
        if not shape(live_catalogue["GeoFootprint"]).equals_exact(shape(row["footprint"]), 1e-10):
            raise ValueError("Current CDSE footprint differs from selected acquisition")
        if not receipt["zip_crc_passed"] or not receipt["manifest_checks"]:
            raise ValueError("Download lacks passed ZIP/manifest integrity checks")
        expected = next(
            c["Value"] for c in live_catalogue["Checksum"] if c["Algorithm"].upper() == "MD5"
        )
        archive = product / (name + ".zip")
        if file_digest(archive) != receipt["archive_sha256"]:
            raise ValueError("Archive changed")
        with archive.open("rb") as f:
            measured_md5 = hashlib.file_digest(f, "md5").hexdigest()
        if (
            measured_md5 != expected
            or archive.stat().st_size != receipt["catalogue"]["ContentLength"]
        ):
            raise ValueError("Archive differs from live catalogue receipt")
        safe = Path(_open_path(product / name))
        if safe_file_inventory(safe) != receipt["safe_files_sha256"]:
            raise ValueError("Extracted SAFE source inventory changed")
        if name not in quality_by_name:
            raise ValueError("Full quality scan missing for " + name)
        p, q = quality_by_name[name]
        binding_path = p.with_suffix(".binding.json")
        if binding_path.is_file():
            binding = json.loads(binding_path.read_text())
            if binding["inputs_and_code"]["safe_files_sha256"] != receipt["safe_files_sha256"]:
                raise ValueError("New quality scan differs from verified SAFE sources")
        row.update(
            status="radiometric_pass"
            if q["passed"]
            else "radiometric_reject"
            if q["channels"]
            else "processing_reject",
            archive_bytes=archive.stat().st_size,
            archive_sha256=receipt["archive_sha256"],
            catalogue_md5_matched=True,
            live_catalogue_url=catalogue_url,
            live_catalogue_verified_utc=datetime.now(UTC).isoformat(),
            zip_crc_passed=receipt["zip_crc_passed"],
            manifest_checks=len(receipt["manifest_checks"]),
            quality=q,
            quality_report_sha256=file_digest(p),
            download_receipt_sha256=file_digest(receipt_path),
        )
        rows.append(row)
    args.output.mkdir(parents=True)
    for folder in ["plans", "quality", "downloads", "comparisons", "metadata"]:
        (args.output / folder).mkdir()
    for p, name in zip(plans, plan_names, strict=True):
        shutil.copyfile(p, args.output / "plans" / name)
    for name, (p, _quality) in quality_by_name.items():
        shutil.copyfile(p, args.output / "quality" / (name + ".json"))
        if p.with_suffix(".binding.json").is_file():
            shutil.copyfile(
                p.with_suffix(".binding.json"),
                args.output / "quality" / (name + ".binding.json"),
            )
    for p in (args.root / "raw").glob("*/download-receipt.json"):
        r = json.loads(p.read_text())
        shutil.copyfile(p, args.output / "downloads" / (r["catalogue"]["Name"] + ".json"))
    for p in args.root.glob("marine-*.json"):
        shutil.copyfile(p, args.output / "quality" / p.name)
    for p in args.root.glob("validation-*/report.json"):
        shutil.copyfile(p, args.output / "comparisons" / (p.parent.name + ".json"))
    if not args.plan:
        for p in Path("data/processed").glob("validation-*.json"):
            shutil.copyfile(p, args.output / "plans" / p.name)
        for folder in [
            "metadata-screen",
            "metadata-screen-ew",
            "february-iw-screen",
            "march4-iw-screen",
            "western-iw-screen",
            "april9-iw-screen",
        ]:
            source = Path("data/processed") / folder
            if source.exists():
                shutil.copytree(source, args.output / "metadata" / folder)
        for name in [
            "catalogue-search.json",
            "recent-ew-metadata-screen.json",
            "recent-ew-annotation.xml",
            "iw-grdm-search.json",
        ]:
            p = Path("data/processed") / name
            if p.is_file():
                shutil.copyfile(p, args.output / "metadata" / name)
    service = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "provider": "Copernicus Data Space Ecosystem",
        "consumed_plans": [
            {"published_file": "plans/" + name, "sha256": file_digest(path)}
            for path, name in zip(plans, plan_names, strict=True)
        ],
        "policy": QualityPolicy().model_dump(),
        "frozen_manifest_sha256": file_digest(Path("docs/evaluation/v1/manifest.json")),
        "aoi_sha256": file_digest(Path("configs/aoi.geojson")),
        "downloaded_products": sum("archive_bytes" in r for r in rows),
        "downloaded_archive_bytes": sum(r.get("archive_bytes", 0) for r in rows),
        "processing_validation_complete": False,
        "deployment_gate_open": False,
        "reference_service_blocker": None
        if service.returncode == 0
        else {
            "reason": "Docker API unavailable at publication; reference execution requires a functioning service"
        },
        "reference_service_version_at_publication": service.stdout.strip()
        if service.returncode == 0
        else None,
        "claim_boundary": "Development suitability search, deliberately adaptive to metadata and rejected quality results. Not an unbiased regional validation sample, sensor certification, precision/recall evaluation, false-alarm rate, or operational coverage claim. Seasonal/whole-footprint/absolute-ground-geolocation limits remain. Historical unbound quality reports are published as recorded executions, not silently converted into new reusable cache acceptance.",
        "products": rows,
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    fig, ax = plt.subplots(figsize=(8, 10), constrained_layout=True)
    aoi = load_aoi()
    xx, yy = aoi.exterior.xy
    ax.plot(xx, yy, color="black", linewidth=1.6, label="NL study polygon")
    for index, row in enumerate(rows, 1):
        geometry = shape(row["footprint"]).intersection(aoi)
        color = {
            "radiometric_pass": "#2474a9",
            "radiometric_reject": "#b34538",
            "processing_reject": "#8d5a32",
            "metadata_excluded_without_full_download": "#777777",
        }[row["status"]]
        polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        for poly in polygons:
            if poly.is_empty:
                continue
            x, y = poly.exterior.xy
            ax.fill(x, y, facecolor=color, alpha=0.10, edgecolor=color, linewidth=0.9)
        point = geometry.representative_point()
        ax.text(point.x, point.y, f"{index}: {row['mode']}", fontsize=8, color=color)
        row["map_label"] = index
    ax.set(
        xlabel="Longitude",
        ylabel="Latitude",
        title="New Sentinel-1 acquisition footprints in the NL study polygon\nBlue: radiometric pass only; red: reject; grey: metadata excluded",
    )
    ax.set_aspect(1.6)
    ax.grid(alpha=0.2)
    fig.text(
        0.5,
        0.015,
        "Footprints are not eligible surveyed area. SNAP qualification remains incomplete.",
        ha="center",
        fontsize=8,
    )
    fig.savefig(args.output / "coverage.png", dpi=160)
    plt.close(fig)
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    (args.output / "coverage.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": r["footprint"],
                        "properties": {
                            "product": r["product"],
                            "mode": r["mode"],
                            "status": r["status"],
                            "map_label": r["map_label"],
                        },
                    }
                    for r in rows
                ],
            },
            indent=2,
        )
    )
    print(
        "Published",
        report["downloaded_products"],
        "authentic downloads; deployment gate remains closed.",
        flush=True,
    )


if __name__ == "__main__":
    main()
