"""Aggregate immutable acquisition evidence without granting a scientific release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import shape

from cryolens.eval.cohort import file_digest
from cryolens.geo.aoi import load_aoi
from cryolens.preprocess.provenance import original_inventory
from cryolens.preprocess.quality import QualityPolicy


def export_coverage(root: Path, rows: list[dict[str, Any]]) -> None:
    path = root / "current-coverage.geojson"
    if path.exists():
        raise FileExistsError("Preserve existing coverage evidence")
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": row["footprint"],
                        "properties": {
                            key: value for key, value in row.items() if key != "footprint"
                        },
                    }
                    for row in rows
                ],
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.report_dir
    if (root / "current-status.json").exists():
        raise FileExistsError("Preserve the current-status snapshot; choose a new version")
    search = json.loads((root / "continuation/report.json").read_text())
    if search["policy"] != QualityPolicy().model_dump():
        raise ValueError("Acquisition search changed the strict quality policy")
    evidence: dict[str, tuple[dict[str, Any], str]] = {}
    for folder in ["ew-northern-qualified", "iw-northern-reference"]:
        path = root / folder / "report.json"
        report = json.loads(path.read_text())
        if len(report["products"]) != 1 or len(report["products"][0]["windows"]) != 9:
            raise ValueError("Representative processing evidence requires nine planned windows")
        product = report["products"][0]
        if product["quality"]["policy"] != QualityPolicy().model_dump():
            raise ValueError("Processing evidence changed the strict quality policy")
        evidence[product["product"]] = (report, path.relative_to(root).as_posix())
    marine = json.loads((root / "ew-northern-qualified/marine-quality.json").read_text())
    ew_report = next(
        report for report, _path in evidence.values() if report["products"][0]["mode"] == "EW"
    )
    if (
        marine.get("schema_version") != 2
        or marine.get("source_inventory_verified_after_scan") is not True
        or marine.get("source_files_sha256")
        != original_inventory(ew_report["products"][0].get("provenance", {}))
        or not marine["passed"]
        or marine["policy"] != QualityPolicy().model_dump()
        or marine["product"] != ew_report["products"][0]["product"]
        or marine["reader_sha256"] != ew_report["validation_source_sha256"]["safe_reader.py"]
    ):
        raise ValueError("EW marine quality is absent, rejected or bound to different processing")
    rows = []
    for source in search["products"]:
        if "archive_bytes" not in source:
            continue
        comparison, path = evidence.get(source["product"], ({}, ""))
        row = {
            "product": source["product"],
            "mode": source["mode"],
            "date": source["acquired_utc"],
            "footprint": source["footprint"],
            "quality_passed": source["quality"]["passed"],
            "nonpositive_HV_fraction": source["quality"]["channels"]["HV"]["nonpositive_fraction"],
            "scientific_comparison_passed": comparison.get("scientific_comparison_passed", False),
            "passing_product_checks": comparison.get("deployment_gate_open", False),
            "comparison_report": path or None,
        }
        if row["passing_product_checks"] and not row["quality_passed"]:
            raise ValueError("Passing product contradicts its full quality rejection")
        rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["product"]))
    for index, row in enumerate(rows, 1):
        row["map_label"] = index
    status = {
        "schema_version": 1,
        "purpose": "Post-hoc adaptive development processing suitability summary",
        "baseline_report_sha256": file_digest(root / "report.json"),
        "continuation_report_sha256": file_digest(root / "continuation/report.json"),
        "downloaded_products": len(rows),
        "downloaded_archive_bytes": search["downloaded_archive_bytes"],
        "strict_policy": QualityPolicy().model_dump(),
        "qualified_modes": sorted({r["mode"] for r in rows if r["passing_product_checks"]}),
        "step2_complete": False,
        "scientific_release_granted": False,
        "deployment_gate_open": False,
        "remaining": [
            "Representative usable IW and southern NL observations",
            "Whole-footprint geometry and seasonal qualification",
            "Independent absolute-geolocation evidence and scientific release",
        ],
        "products": rows,
    }
    (root / "current-status.json").write_text(json.dumps(status, indent=2, allow_nan=False))
    export_coverage(root, rows)
    fig, ax = plt.subplots(figsize=(9, 11), constrained_layout=True)
    aoi = load_aoi()
    x, y = aoi.exterior.xy
    ax.plot(x, y, color="#303030", linewidth=1.5)
    points = []
    for row in rows:
        geometry = shape(row["footprint"]).intersection(aoi)
        color = (
            "#167443"
            if row["passing_product_checks"]
            else "#ad781d"
            if row["quality_passed"]
            else "#ae403e"
        )
        polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        for poly in polygons:
            x, y = poly.exterior.xy
            ax.fill(x, y, facecolor=color, alpha=0.12, edgecolor=color, linewidth=0.8)
        point = geometry.representative_point()
        points.append((row, point, color))
    points.sort(key=lambda v: v[1].y, reverse=True)
    for index, (row, point, color) in enumerate(points):
        label_lat = 60.0 - index * 0.75 if point.y > 50 else point.y
        ax.annotate(
            f"{row['map_label']} · {row['mode']} · {row['date'][:10]}",
            (point.x, point.y),
            xytext=(-65.2, label_lat),
            color=color,
            fontsize=8,
            arrowprops={"arrowstyle": "-", "color": color, "lw": 0.65},
        )
    ax.set(
        xlabel="Longitude",
        ylabel="Latitude",
        xlim=(-65.5, -43),
        ylim=(42, 61.2),
        title=f"{len(rows)} authentic Sentinel-1 acquisitions in the NL study polygon\nGreen: product checks pass · Amber: quality only · Red: quality rejection",
    )
    ax.set_aspect(1.55)
    ax.grid(alpha=0.2)
    fig.text(
        0.5,
        0.015,
        "Footprints are not eligible surveyed area. One EW passes; IW and regional deployment remain blocked.",
        ha="center",
        fontsize=8,
    )
    fig.savefig(root / "current-coverage.png", dpi=160)
    plt.close(fig)
    print("Published current status; qualified modes:", status["qualified_modes"])


if __name__ == "__main__":
    main()
