"""Public Sentinel-2 L2A discovery and lossless native-window acquisition."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import planetary_computer
import rasterio
import requests
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping, shape

from cryolens.eval.cohort import file_digest
from cryolens.geo.aoi import load_aoi

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
BANDS = ("B02", "B03", "B04", "B08", "SCL")


def utc(value: datetime | str) -> datetime:
    result = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if result.tzinfo is None:
        raise ValueError("Acquisition time requires a timezone")
    return result.astimezone(UTC)


class Sentinel2Client:
    """Search all local overlaps; scene cloud percentages never decide visibility."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def search(
        self, footprint: dict[str, Any], acquired: datetime, hours: float = 12
    ) -> dict[str, Any]:
        region = shape(footprint).intersection(load_aoi())
        if region.is_empty:
            raise ValueError("Optical search must overlap the NL study polygon")
        acquired = utc(acquired)
        interval = timedelta(hours=hours)
        body = {
            "collections": ["sentinel-2-l2a"],
            "intersects": mapping(region),
            "datetime": f"{(acquired - interval).isoformat()}/{(acquired + interval).isoformat()}",
            "limit": 100,
        }
        url, method, payload = STAC_URL + "/search", "POST", body
        items: dict[str, dict[str, Any]] = {}
        for _ in range(50):
            response = self.session.request(
                method, url, json=payload if method == "POST" else None, timeout=60
            )
            response.raise_for_status()
            page = response.json()
            for feature in page["features"]:
                if feature.get("collection") != "sentinel-2-l2a":
                    continue
                when = utc(feature["properties"]["datetime"])
                if abs((when - acquired).total_seconds()) > hours * 3600 or not shape(
                    feature["geometry"]
                ).intersects(region):
                    continue
                assets = feature.get("assets", {})
                item = {
                    "id": feature["id"],
                    "geometry": feature["geometry"],
                    "properties": {
                        k: v
                        for k, v in feature["properties"].items()
                        if k
                        in {
                            "datetime",
                            "platform",
                            "eo:cloud_cover",
                            "s2:processing_baseline",
                            "s2:product_uri",
                            "proj:epsg",
                            "proj:code",
                        }
                    },
                    "assets": {k: assets[k] for k in BANDS if k in assets},
                }
                # Never persist temporary SAS credentials.
                for asset in item["assets"].values():
                    asset["href"] = asset["href"].split("?")[0]
                items[item["id"]] = item
            next_link = next(
                (link for link in page.get("links", []) if link.get("rel") == "next"), None
            )
            if next_link is None:
                break
            url = next_link["href"]
            if urlsplit(url).hostname != "planetarycomputer.microsoft.com":
                raise ValueError("Unexpected catalogue pagination host")
            method = next_link.get("method", "GET")
            payload = next_link.get("body", {})
        else:
            raise RuntimeError("Optical catalogue pagination incomplete; do not report no coverage")
        return {
            "endpoint": STAC_URL,
            "query": body,
            "queried_at": datetime.now(UTC).isoformat(),
            "complete": True,
            "items": sorted(
                items.values(),
                key=lambda i: (
                    abs((utc(i["properties"]["datetime"]) - acquired).total_seconds()),
                    i["id"],
                ),
            ),
        }

    def download_windows(
        self, item: dict[str, Any], bounds_3978: tuple[float, float, float, float], output: Path
    ) -> dict[str, Any]:
        """Range-read real COG pixels and preserve native CRS, grid and masks.

        These are bounded native source crops, not whole-product downloads.
        Hashes certify local crop bytes; they are not publisher full-file hashes.
        """
        output.mkdir(parents=True, exist_ok=True)
        missing = set(BANDS) - set(item["assets"])
        if missing:
            raise ValueError("Optical item lacks required assets: " + ",".join(sorted(missing)))
        receipt: dict[str, Any] = {}
        for band in BANDS:
            asset = item["assets"][band]
            href = asset["href"]
            parsed = urlsplit(href)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(
                ".blob.core.windows.net"
            ):
                raise ValueError("Optical assets must use the public Azure HTTPS source")
            signed = planetary_computer.sign_url(href)
            with rasterio.Env(
                GDAL_HTTP_TIMEOUT="60", GDAL_HTTP_MAX_RETRY="2", GDAL_HTTP_RETRY_DELAY="2"
            ):
                with rasterio.open(signed) as source:
                    if source.crs is None or source.count != 1:
                        raise ValueError("Optical source lacks a single georeferenced band")
                    expected = 20 if band == "SCL" else 10
                    if (
                        abs(abs(source.transform.a) - expected) > 0.01
                        or abs(abs(source.transform.e) - expected) > 0.01
                    ):
                        raise ValueError("Unexpected optical native pixel spacing")
                    native_bounds = transform_bounds(
                        "EPSG:3978", source.crs, *bounds_3978, densify_pts=21
                    )
                    floating = from_bounds(*native_bounds, transform=source.transform)
                    # Include every intersecting native pixel without resampling.
                    import math

                    left, top = math.floor(floating.col_off), math.floor(floating.row_off)
                    right, bottom = (
                        math.ceil(floating.col_off + floating.width),
                        math.ceil(floating.row_off + floating.height),
                    )
                    window = Window(left, top, right - left, bottom - top).intersection(
                        Window(0, 0, source.width, source.height)
                    )
                    if window.width * window.height > 4_000_000:
                        raise ValueError("Optical native crop exceeds the bounded review budget")
                    values = source.read(1, window=window)
                    mask = source.read_masks(1, window=window)
                    path = output / f"{band}-native.tif"
                    profile = source.profile.copy()
                    profile.update(
                        driver="GTiff",
                        width=values.shape[1],
                        height=values.shape[0],
                        transform=source.window_transform(window),
                        compress="deflate",
                        tiled=False,
                    )
                    profile.pop("blockxsize", None)
                    profile.pop("blockysize", None)
                    with (
                        rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True),
                        rasterio.open(path, "w", **profile) as target,
                    ):
                        target.write(values, 1)
                        target.write_mask(mask)
                    receipt[band] = {
                        "filename": path.name,
                        "source_href": href,
                        "sha256": file_digest(path),
                        "native_pixel_spacing_m": expected,
                        "native_crs": str(source.crs),
                        "source_window": [
                            window.row_off,
                            window.col_off,
                            window.height,
                            window.width,
                        ],
                        "source_shape": [source.height, source.width],
                        "transform": list(source.window_transform(window)),
                        "raster_band_metadata": asset.get("raster:bands", []),
                        "download_kind": "lossless_native_window",
                        "source_resampling": "none",
                    }
        (output / "item.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
        return receipt
