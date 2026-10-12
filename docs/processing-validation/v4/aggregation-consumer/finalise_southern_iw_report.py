"""Summarize source-bound southern IW investigations without a regional release."""

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
from validation.finalise_acquisition_report import export_coverage
from validation.publish_validation_run import verify_execution_sources


def processing_evidence(
    root: Path, source: dict[str, Any], folder: str, acquisition_folder: str = "acquisition-search"
) -> dict[str, Any]:
    if Path(folder).name != folder or folder in {".", ".."} or "\\" in folder:
        raise ValueError("Investigation folder must be a basename")
    path = root / folder / "report.json"
    report = json.loads(path.read_text())
    if len(report["products"]) != 1:
        raise ValueError("One source per processing investigation is required")
    product = report["products"][0]
    if product["product"] != source["product"] or product["mode"] != source["mode"]:
        raise ValueError("Processing source identity differs")
    quality = product["quality"]
    if (
        quality.get("scope") != "full_measurement_grid"
        or quality.get("policy") != QualityPolicy().model_dump()
    ):
        raise ValueError("Processing quality scope or policy changed")
    if (
        quality["channels"] != source["quality"]["channels"]
        or quality["passed"] != source["quality"]["passed"]
    ):
        raise ValueError("Independent full scans disagree")
    receipt_path = root / acquisition_folder / "downloads" / (source["product"] + ".json")
    if file_digest(receipt_path) != source["download_receipt_sha256"]:
        raise ValueError("Download receipt changed")
    receipt = json.loads(receipt_path.read_text())
    if (
        receipt["catalogue"]["Id"] != source["catalogue_id"]
        or receipt["catalogue"]["Name"] != source["product"]
    ):
        raise ValueError("Download catalogue identity differs")
    original = original_inventory(product["provenance"])
    consumed = {
        k: v
        for k, v in receipt["safe_files_sha256"].items()
        if k == "manifest.safe"
        or (k.startswith("annotation/") and k.endswith(".xml"))
        or (Path(k).parent.as_posix() == "measurement" and k.endswith(".tiff"))
    }
    if consumed != original:
        raise ValueError("Processing source inventory differs from download")
    configuration = root / folder / "configuration.json"
    if file_digest(configuration) != report["configuration_sha256"]:
        raise ValueError("Processing configuration changed")
    config = json.loads(configuration.read_text())
    fractions = [[r, c] for r in (0.1, 0.5, 0.9) for c in (0.1, 0.5, 0.9)]
    if config.get("window_size_px") != 512 or config.get("sampling_fractions") != fractions:
        raise ValueError("Fixed nine-window sampling protocol changed")
    verify_execution_sources(report, root / folder)
    stripes = quality["stripes"]
    height = max(s["window"][0] + s["window"][2] for s in stripes)
    width = max(s["window"][1] + s["window"][3] for s in stripes)
    expected_windows = [
        [round((height - 512) * r), round((width - 512) * c), 512, 512] for r, c in fractions
    ]
    windows = product["windows"]
    if [w["window"] for w in windows] != expected_windows:
        raise ValueError("All nine fixed processing comparisons are required")
    required = {w["reference_receipt_sha256"] for w in windows}
    receipts = {
        file_digest(p): json.loads(p.read_text())
        for p in (root / folder / "reference-receipts").glob("*-receipt.json")
    }
    if len(required) != 9 or not required <= receipts.keys():
        raise ValueError("Missing or changed reference receipts")
    expected_checks = {
        "calibration_HH",
        "calibration_HV",
        "noise_HH",
        "noise_HV",
        "annotation_geolocation",
        "incidence",
        "cog_integrity",
        "registration_HH",
        "registration_HV",
        "reprojection_HH",
        "reprojection_HV",
        "diagnostic_scope",
    }
    for window in windows:
        checks = window.get("checks", {})
        if set(checks) != expected_checks or window["scientific_comparison_passed"] != all(
            c["passed"] for c in checks.values()
        ):
            raise ValueError("Window summary contradicts individual scientific checks")
        reference = receipts[window["reference_receipt_sha256"]]
        if (
            reference["window"] != window["window"]
            or reference["graph_sha256"] != report["graph_sha256"]
            or reference["image_id"] != report["reference_image_id"]
        ):
            raise ValueError("Reference execution binding differs")
    scientific = all(w["scientific_comparison_passed"] for w in windows)
    if (
        scientific != report["scientific_comparison_passed"]
        or scientific != product["scientific_comparison_passed"]
    ):
        raise ValueError("Comparison summary differs from recorded windows")
    if report["deployment_gate_open"] != (scientific and quality["passed"]):
        raise ValueError("Product processing gate contradicts full quality/comparisons")
    marine_path = root / folder / "marine-quality.json"
    marine_passed = False
    if marine_path.is_file():
        marine = json.loads(marine_path.read_text())
        if (
            marine.get("schema_version") != 2
            or marine.get("source_inventory_verified_after_scan") is not True
            or marine.get("scope") != "nl_aoi_marine_measurement_grid"
            or marine.get("coastal_buffer_m") != 500
            or marine.get("aoi_sha256") != file_digest(Path("configs/aoi.geojson"))
            or marine.get("shoreline_sha256")
            != "c3d44eb262868d63ab8d3a16f926d2360636f7a7c52d5db233eb7e729d802e28"
            or marine.get("source_files_sha256") != original
            or marine["policy"] != QualityPolicy().model_dump()
            or marine["product"] != source["product"]
            or marine["reader_sha256"] != report["validation_source_sha256"]["safe_reader.py"]
        ):
            raise ValueError("Marine report is unbound or differs from processing")
        if set(marine["channels"]) != {"HH", "HV"}:
            raise ValueError("Both marine polarization summaries are required")
        marine_conditions = []
        for channel in marine["channels"].values():
            finite = channel["finite_pixels"]
            population = channel["marine_pixel_centres"]
            nonpositive = channel["nonpositive_pixels"]
            if (
                min(finite, population, nonpositive, channel["invalid_incidence_pixels"]) < 0
                or finite > population
                or nonpositive > finite
            ):
                raise ValueError("Invalid marine counts")
            fraction = nonpositive / finite if finite else 1.0
            coverage = finite / population if population else 0.0
            if channel["nonpositive_fraction"] != fraction or channel["valid_fraction"] != coverage:
                raise ValueError("Marine channel statistics are inconsistent")
            marine_conditions.append(
                fraction <= QualityPolicy().max_nonpositive_fraction
                and coverage >= QualityPolicy().min_valid_fraction
                and not channel["invalid_incidence_pixels"]
            )
        expected_marine_pass = all(marine_conditions) and not marine["reasons"]
        if marine["passed"] != expected_marine_pass:
            raise ValueError("Marine summary contradicts strict channel checks")
        marine_passed = expected_marine_pass
    return {
        "scientific_comparison_passed": scientific,
        "marine_quality_passed": marine_passed,
        "passing_product_checks": report["deployment_gate_open"] and marine_passed,
        "comparison_report": path.relative_to(root).as_posix(),
        "comparison_report_sha256": file_digest(path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.report_dir
    outputs = [
        root / name
        for name in ("current-status.json", "current-coverage.geojson", "current-coverage.png")
    ]
    if any(p.exists() for p in outputs):
        raise FileExistsError("Preserve current evidence; use a fresh publication version")
    manifest_path = root / "processing-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    acquisition_folder = manifest.get("acquisition_folder", "acquisition-search")
    if (
        Path(acquisition_folder).name != acquisition_folder
        or acquisition_folder in {".", ".."}
        or "\\" in acquisition_folder
    ):
        raise ValueError("Acquisition folder must be a basename")
    search_path = root / acquisition_folder / "report.json"
    search = json.loads(search_path.read_text())
    if search["policy"] != QualityPolicy().model_dump():
        raise ValueError("Acquisition policy changed")
    investigations = manifest["investigations"]
    rows = []
    seen = set()
    for source in search["products"]:
        name = source["product"]
        if Path(name).name != name or "\\" in name or name in seen:
            raise ValueError("Invalid or duplicate acquisition product")
        seen.add(name)
        quality = source["quality"]
        if (
            quality.get("policy") != QualityPolicy().model_dump()
            or quality.get("scope") != "full_measurement_grid"
        ):
            raise ValueError("Unqualified full-grid acquisition quality")
        evidence = {
            "scientific_comparison_passed": None,
            "marine_quality_passed": None,
            "passing_product_checks": False,
            "comparison_report": None,
        }
        if source["product"] in investigations:
            evidence = processing_evidence(
                root, source, investigations[source["product"]], acquisition_folder
            )
        rows.append(
            {
                "product": source["product"],
                "mode": source["mode"],
                "date": source["acquired_utc"],
                "footprint": source["footprint"],
                "quality_passed": quality["passed"],
                "quality_reasons": quality["reasons"],
                "nonpositive_HV_fraction": quality.get("channels", {})
                .get("HV", {})
                .get("nonpositive_fraction"),
                **evidence,
            }
        )
    rows.sort(key=lambda r: (r["date"], r["product"]))
    for number, row in enumerate(rows, 1):
        row["map_label"] = number
    summary = {
        "schema_version": 1,
        "purpose": "Adaptive southern IW development processing evidence; no regional operational release",
        "acquisition_report_sha256": file_digest(search_path),
        "processing_manifest_sha256": file_digest(manifest_path),
        "downloaded_products": len(rows),
        "downloaded_archive_bytes": search["downloaded_archive_bytes"],
        "strict_policy": QualityPolicy().model_dump(),
        "qualified_modes_in_this_investigation": sorted(
            {r["mode"] for r in rows if r["passing_product_checks"]}
        ),
        "southern_iw_product_processing_demonstrated": any(
            r["passing_product_checks"] for r in rows
        ),
        "step2_complete": False,
        "deployment_gate_open": False,
        "scientific_release_granted": False,
        "remaining": [
            "Broader representative seasonal and regional availability",
            "Independent absolute field geolocation and scientific release",
        ],
        "claim_boundary": "Per-product full-grid and marine quality plus matched reference comparisons. Sparse full-footprint metadata diagnostics are not independent ground truth. All rejected outcomes retained; sampled weather is selection context, not image truth or regional availability estimation.",
        "products": rows,
    }
    with outputs[0].open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=2, allow_nan=False)
    export_coverage(root, rows)
    fig, ax = plt.subplots(figsize=(9, 7), constrained_layout=True)
    aoi = load_aoi()
    x, y = aoi.exterior.xy
    ax.plot(x, y, color="#303030", label="NL research polygon")
    for row in rows:
        clipped = shape(row["footprint"]).intersection(aoi)
        color = (
            "#167443"
            if row["passing_product_checks"]
            else "#ad781d"
            if row["quality_passed"]
            else "#ae403e"
        )
        polygons = list(clipped.geoms) if clipped.geom_type == "MultiPolygon" else [clipped]
        for polygon in polygons:
            x, y = polygon.exterior.xy
            ax.fill(x, y, facecolor=color, edgecolor=color, alpha=0.15)
        point = clipped.representative_point()
        ax.text(point.x, point.y, f"{row['map_label']}: {row['date'][:10]}", fontsize=8)
    ax.set(
        xlim=(-57, -46),
        ylim=(43, 50),
        xlabel="Longitude",
        ylabel="Latitude",
        title="Southern Newfoundland IW processing investigation\nGreen: per-product checks pass; red: strict quality rejection",
    )
    ax.set_aspect(1.5)
    ax.grid(alpha=0.2)
    fig.savefig(outputs[2], dpi=160)
    plt.close(fig)
    print(
        "Qualified IW examples:",
        sum(row["passing_product_checks"] for row in rows),
        "regional/scientific release gates remain closed",
        flush=True,
    )


if __name__ == "__main__":
    main()
