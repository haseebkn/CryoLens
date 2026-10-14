"""Shared fail-closed pixel and visibility checks for optical evidence."""

from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from cryolens.geo.aoi import contains_point, raster_aoi_mask
from cryolens.review.pairs import assess_visibility
from cryolens.review.policy import PairingPolicy


def valid_point(
    path: Path, position: tuple[float, float], bands: list[int], cloud_buffer_m: float = 0
) -> None:
    if not contains_point(*position):
        raise ValueError("Observation must lie in the NL study polygon")
    with rasterio.open(path) as src:
        x, y = Transformer.from_crs(4326, src.crs, always_xy=True).transform(*position)
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            raise ValueError("Observation lies outside inspected imagery")
        optical = bands == [1, 2, 3, 4, 5]
        spacing = abs(src.transform.a)
        if optical and (
            src.crs.to_epsg() != 3978
            or src.transform.b != 0
            or src.transform.d != 0
            or spacing != abs(src.transform.e)
        ):
            raise ValueError("Optical buffer requires the projected square review grid")
        radius = int(np.ceil(cloud_buffer_m / spacing)) if optical else 0
        if not (radius <= row < src.height - radius and radius <= col < src.width - radius):
            raise ValueError("Optical cloud/shadow buffer has incomplete image support")
        data = src.read(
            bands,
            window=Window(col - radius, row - radius, 2 * radius + 1, 2 * radius + 1),
            masked=True,
        )
        point = data[:, radius, radius]
        if np.ma.getmaskarray(point).any() or not np.isfinite(point.data).all():
            raise ValueError("Observation has no valid image pixels")
        if optical:
            valid = (
                ~np.ma.getmaskarray(data).any(axis=0)
                & np.isfinite(data.data).all(axis=0)
                & (data.data[:3] > 0).all(axis=0)
            )
            domain = np.zeros(valid.shape, dtype=bool)
            domain[radius, radius] = True
            result = assess_visibility(
                data.data[-1],
                valid,
                domain,
                PairingPolicy(cloud_buffer_m=cloud_buffer_m, review_grid_spacing_m=spacing),
            )
            if result["visible_fraction"] != 1:
                raise ValueError(
                    "Optical position is obscured, SCL-ambiguous or has incomplete cloud/shadow buffer support"
                )


def screened_visibility(path: Path, pair: dict[str, Any]) -> dict[str, Any]:
    policy = PairingPolicy.model_validate(pair.get("policy", {}))
    with rasterio.open(path) as src:
        if (
            src.crs.to_epsg() != 3978
            or src.transform.a != policy.review_grid_spacing_m
            or src.transform.e != -policy.review_grid_spacing_m
            or src.transform.b != 0
            or src.transform.d != 0
        ):
            raise ValueError("Optical screening requires the recorded projected review grid")
        data = src.read(masked=True)
        valid = (
            ~np.ma.getmaskarray(data).any(axis=0)
            & np.isfinite(data.data).all(axis=0)
            & (data.data[:3] > 0).all(axis=0)
        )
        x, y = Transformer.from_crs(4326, src.crs, always_xy=True).transform(
            *pair["candidate_lonlat"]
        )
        rows, cols = np.ogrid[: src.height, : src.width]
        radius = min(
            pair.get("review_radius_m", src.width * src.transform.a / 2), pair["matching_radius_m"]
        )
        domain = (
            (src.transform.c + (cols + 0.5) * src.transform.a - x) ** 2
            + (src.transform.f + (rows + 0.5) * src.transform.e - y) ** 2
        ) <= radius**2
        domain &= raster_aoi_mask((src.height, src.width), src.transform, src.crs)
        return assess_visibility(data.data[-1], valid, domain, policy)
