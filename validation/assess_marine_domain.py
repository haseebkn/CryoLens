"""Measure signed-power quality within the NL polygon outside buffered GSHHG land."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import shapely
from shapely.ops import transform, unary_union

from cryolens.eval.cohort import file_digest
from cryolens.geo.aoi import points_in_aoi
from cryolens.preprocess.masks import LandMaskGenerator
from cryolens.preprocess.quality import QualityPolicy
from cryolens.preprocess.safe_reader import SAFEProductReader, _parse_product_annotation
from cryolens.preprocess.validation import require_development_product


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--safe", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--shoreline", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve existing report; choose a new output")
    require_development_product(args.safe, Path("docs/evaluation/v1/manifest.json"))
    policy = QualityPolicy()
    coast = LandMaskGenerator(cache_path=args.shoreline).load_geometries()
    forward = pyproj.Transformer.from_crs(4326, 3978, always_xy=True).transform
    inverse = pyproj.Transformer.from_crs(3978, 4326, always_xy=True).transform
    land = transform(inverse, transform(forward, unary_union(coast)).buffer(500))
    shapely.prepare(land)
    reader = SAFEProductReader(args.safe)
    meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
    height, width = meta["n_lines"], meta["n_samples"]
    counts: dict[str, dict[str, Any]] = {
        pol: {
            "marine_pixel_centres": 0,
            "finite_pixels": 0,
            "nonpositive_pixels": 0,
            "invalid_incidence_pixels": 0,
        }
        for pol in ["HH", "HV"]
    }
    for start in range(0, height, 32):
        window = (start, 0, min(32, height - start), width)
        for pol in ["HH", "HV"]:
            channel = reader.read_sigma0(pol, window=window)
            lon, lat = channel["longitude"], channel["latitude"]
            domain = points_in_aoi(lon, lat) & ~shapely.intersects_xy(land, lon, lat)
            values = channel["sigma0_linear"]
            finite = domain & np.isfinite(values)
            angles = channel["incidence_angle_deg"]
            row = counts[pol]
            row["marine_pixel_centres"] += int(domain.sum())
            row["finite_pixels"] += int(finite.sum())
            row["nonpositive_pixels"] += int((finite & (values <= 0)).sum())
            row["invalid_incidence_pixels"] += int(
                (
                    finite
                    & (
                        ~np.isfinite(angles)
                        | (angles < policy.incidence_min_deg)
                        | (angles > policy.incidence_max_deg)
                    )
                ).sum()
            )
        if start % (32 * 80) == 0:
            print("Marine measurement scan row", start, "/", height, flush=True)
    reasons = []
    for pol, row in counts.items():
        row["nonpositive_fraction"] = (
            row["nonpositive_pixels"] / row["finite_pixels"] if row["finite_pixels"] else 1.0
        )
        row["valid_fraction"] = (
            row["finite_pixels"] / row["marine_pixel_centres"]
            if row["marine_pixel_centres"]
            else 0.0
        )
        if row["nonpositive_fraction"] > policy.max_nonpositive_fraction:
            reasons.append(pol + ": exceeds unchanged nonpositive-power limit")
        if row["valid_fraction"] < policy.min_valid_fraction:
            reasons.append(pol + ": insufficient valid marine observations")
        if row["invalid_incidence_pixels"]:
            reasons.append(pol + ": invalid incidence angles")
    report = {
        "schema_version": 1,
        "product": reader.safe_dir.name,
        "scope": "nl_aoi_marine_measurement_grid",
        "coastal_buffer_m": 500,
        "shoreline_sha256": file_digest(args.shoreline),
        "aoi_sha256": file_digest(Path("configs/aoi.geojson")),
        "policy": policy.model_dump(),
        "driver_sha256": file_digest(Path(__file__)),
        "reader_sha256": file_digest(Path("src/cryolens/preprocess/safe_reader.py")),
        "channels": counts,
        "passed": not reasons,
        "reasons": reasons,
        "claim_boundary": "Full source-grid marine-domain quality only; no sea-ice exclusion, CFAR support, surveyed eligible area or detection performance. Buffered shoreline and pixel centres use annotation geolocation; absolute field accuracy remains unestablished.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
