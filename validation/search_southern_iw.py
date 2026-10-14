"""Record complete CDSE metadata searches for southern-NL IW development products."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from shapely.geometry import box, shape

from cryolens.eval.cohort import file_digest, load_manifest
from cryolens.geo.aoi import load_aoi
from validation.acquire_sentinel1 import guard

ENDPOINT = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
SOUTH_BOUNDS = (-58.5, 42.5, -44.0, 49.0)


def search(start: str, end: str) -> dict[str, Any]:
    """Query metadata only, recording every page and enforcing the frozen split."""
    south = load_aoi().intersection(box(*SOUTH_BOUNDS))
    manifest_path = Path("docs/evaluation/v1/manifest.json")
    manifest = load_manifest(manifest_path)
    query = (
        "Collection/Name eq 'SENTINEL-1' and contains(Name,'_IW_GRDH_1SDH_') "
        "and endswith(Name,'_COG.SAFE') "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{south.wkt}') "
        f"and ContentDate/Start ge {start}T00:00:00.000Z "
        f"and ContentDate/Start lt {end}T00:00:00.000Z"
    )
    params: dict[str, Any] | None = {
        "$filter": query,
        "$top": 100,
        "$expand": "Attributes",
        "$orderby": "ContentDate/Start asc",
    }
    pages = []
    candidates = []
    excluded = []
    seen = set()
    seen_pages = set()
    url = ENDPOINT
    with httpx.Client(timeout=120) as client:
        while url:
            if (
                urlsplit(url).hostname != "catalogue.dataspace.copernicus.eu"
                or urlsplit(url).scheme != "https"
            ):
                raise ValueError("Unexpected catalogue pagination endpoint")
            response = client.get(url, params=params)
            response.raise_for_status()
            if str(response.url) in seen_pages:
                raise ValueError("Catalogue pagination cycle")
            seen_pages.add(str(response.url))
            payload = response.json()
            pages.append({"request_url": str(response.url), "response": payload})
            for item in payload["value"]:
                if item["Id"] in seen:
                    continue
                seen.add(item["Id"])
                footprint = shape(item["GeoFootprint"])
                overlap = footprint.intersection(south)
                if overlap.is_empty or footprint.area <= 0:
                    continue
                record = {
                    "id": item["Id"],
                    "name": item["Name"],
                    "mode": "IW",
                    "date": item["ContentDate"]["Start"],
                    "bytes": item["ContentLength"],
                    "aoi_fraction": footprint.intersection(load_aoi()).area / footprint.area,
                    "southern_fraction": overlap.area / footprint.area,
                    "overlap_centroid": [overlap.centroid.x, overlap.centroid.y],
                    "attributes": {a["Name"]: a["Value"] for a in item.get("Attributes", [])},
                    "catalogue": item,
                }
                try:
                    guard(record, manifest)
                except ValueError as error:
                    excluded.append({"id": item["Id"], "name": item["Name"], "reason": str(error)})
                else:
                    candidates.append(record)
            url = payload.get("@odata.nextLink", "")
            params = None
    return {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "purpose": "Adaptive development processing search; no regional performance claim",
        "dates": [start, end],
        "southern_bounds": list(SOUTH_BOUNDS),
        "aoi_sha256": file_digest(Path("configs/aoi.geojson")),
        "frozen_manifest_sha256": file_digest(manifest_path),
        "complete_pagination": True,
        "pages": pages,
        "candidates": candidates,
        "excluded": excluded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a fresh catalogue evidence destination")
    result = search(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
    print(
        "Pages:",
        len(result["pages"]),
        "eligible candidates:",
        len(result["candidates"]),
        flush=True,
    )
    ranked = sorted(
        result["candidates"], key=lambda r: (-r["southern_fraction"], r["date"], r["name"])
    )
    for row in ranked[:12]:
        print(
            row["name"],
            round(row["bytes"] / 1e6, 1),
            "MB",
            round(row["southern_fraction"], 3),
            row["overlap_centroid"],
            flush=True,
        )


if __name__ == "__main__":
    main()
