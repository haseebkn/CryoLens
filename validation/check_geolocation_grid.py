"""Check all annotated geolocation-grid points without claiming field accuracy."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.marine_geocoding import MarineGeocoder
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path
from cryolens.preprocess.validation import require_development_product


def required_text(node: ET.Element | None, tag: str) -> str:
    value = node.findtext(tag) if node is not None else None
    if value is None:
        raise ValueError("Missing geolocation field: " + tag)
    return value


def grid_values(points: list[ET.Element], tag: str) -> np.ndarray:
    if not points:
        raise ValueError("Missing geolocation grid")
    return np.array([float(required_text(point, tag)) for point in points], dtype=np.float64)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--safe", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve previous geometry diagnostics")
    require_development_product(args.safe, Path("docs/evaluation/v1/manifest.json"))
    reader = SAFEProductReader(args.safe)
    channels = {}
    for pol in ("HH", "HV"):
        annotation = Path(_open_path(reader._find_for_polarisation(pol)[1]))
        before = file_digest(annotation)
        xml = ET.parse(annotation).getroot()
        points = xml.findall(".//geolocationGrid/geolocationGridPointList/geolocationGridPoint")

        geocoder = MarineGeocoder(annotation)
        rows, cols, supported = geocoder.coordinates(
            grid_values(points, "longitude"),
            grid_values(points, "latitude"),
            (0, 0, *geocoder.full_shape),
        )
        dr, dc = rows - grid_values(points, "line"), cols - grid_values(points, "pixel")
        info = xml.find(".//imageInformation")
        azimuth_spacing = float(required_text(info, "azimuthPixelSpacing"))
        range_spacing = float(required_text(info, "rangePixelSpacing"))
        error = np.hypot(dr * azimuth_spacing, dc * range_spacing)
        finite = np.isfinite(error)
        if not finite.all():
            raise ValueError("Nonfinite source-grid residuals")
        channel = {
            "annotation_sha256": before,
            "grid_points": len(points),
            "supported_points": int(supported.sum()),
            "pixel_spacing_m": [azimuth_spacing, range_spacing],
            "row_error_px_percentiles": np.percentile(np.abs(dr), [50, 95, 100]).tolist(),
            "column_error_px_percentiles": np.percentile(np.abs(dc), [50, 95, 100]).tolist(),
            "nominal_source_distance_error_m_percentiles": np.percentile(
                error, [50, 95, 100]
            ).tolist(),
            "grid_extent": {
                "rows": [
                    float(grid_values(points, "line").min()),
                    float(grid_values(points, "line").max()),
                ],
                "columns": [
                    float(grid_values(points, "pixel").min()),
                    float(grid_values(points, "pixel").max()),
                ],
            },
            "geometry": geocoder.provenance(),
        }
        if file_digest(annotation) != before:
            raise ValueError("Geometry source changed during diagnostic")
        channels[pol] = channel
        print(
            pol,
            "grid",
            len(points),
            "nominal residual m median/p95/max",
            channel["nominal_source_distance_error_m_percentiles"],
            flush=True,
        )
    report = {
        "schema_version": 1,
        "product": reader.safe_dir.name,
        "driver_sha256": file_digest(Path(__file__)),
        "channels": channels,
        "scope": "all manufacturer annotation grid points across original footprint; sparse metadata, not every image pixel",
        "claim_boundary": "Consistency with product geolocation metadata using the same annotated orbit. Nominal source-grid metres are not independently measured ground error. No absolute field accuracy or scientific release is established.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
