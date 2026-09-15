"""The NL shelf research area, shared by ingestion, analysis and serving.

This hand-defined study polygon is not a provincial or maritime jurisdiction
boundary. Land/coastal exclusion is a separate required processing mask.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pyproj
import shapely
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from cryolens.config.settings import get_project_config


@lru_cache(maxsize=1)
def load_aoi() -> BaseGeometry:
    """Read the primary configured study polygon, excluding subregion overlays."""
    path = Path(get_project_config().spatial.aoi_file)
    if not path.is_file():
        path = Path(__file__).resolve().parents[3] / path
    if not path.is_file():
        path = (
            Path(__file__).resolve().parents[1]
            / "resources"
            / get_project_config().spatial.aoi_file
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    features = data.get("features", [])
    feature = next(
        (
            f
            for f in features
            if f.get("properties", {}).get("id") == "newfoundland_labrador_marine"
        ),
        None,
    )
    if feature is None:
        raise ValueError("AOI must include the newfoundland_labrador_marine feature")
    geom = shape(feature["geometry"])
    if geom.is_empty or not geom.is_valid or geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Study area must be a nonempty valid polygon")
    return geom


load_project_aoi = load_aoi


def contains_point(lon: float, lat: float) -> bool:
    """Test a WGS84 point against the study area (boundary included)."""
    return bool(np.isfinite(lon) and np.isfinite(lat) and load_aoi().covers(Point(lon, lat)))


def points_in_aoi(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Test co-registered WGS84 pixel centres without allocating point objects."""
    lon, lat = np.asarray(lon), np.asarray(lat)
    if lon.shape != lat.shape:
        raise ValueError("Longitude and latitude must have matching shapes")
    return np.asarray(
        np.isfinite(lon) & np.isfinite(lat) & shapely.intersects_xy(load_aoi(), lon, lat)
    )


def scene_intersects_aoi(geometry: dict[str, Any] | BaseGeometry) -> bool:
    geom = shape(geometry) if isinstance(geometry, dict) else geometry
    return bool(geom.is_valid and not geom.is_empty and geom.intersects(load_aoi()))


def raster_aoi_mask(shape: tuple[int, int], transform: Any, crs: Any) -> np.ndarray:
    """Test actual WGS84 pixel centres in bounded memory.

    Inverse projection avoids changing the study boundary by connecting widely
    spaced projected vertices with straight chords. Chunking bounds memory for
    full Sentinel-1 swaths and supports rotated affine grids.
    """
    if crs is None:
        raise ValueError("A raster CRS is required for geographic restriction")
    transformer = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    output = np.zeros(shape, dtype=bool)
    cols = np.arange(shape[1], dtype=float)[None, :] + 0.5
    for start in range(0, shape[0], 256):
        rows = np.arange(start, min(start + 256, shape[0]), dtype=float)[:, None] + 0.5
        x = transform.a * cols + transform.b * rows + transform.c
        y = transform.d * cols + transform.e * rows + transform.f
        lon, lat = transformer.transform(x, y)
        output[start : start + rows.size] = points_in_aoi(lon, lat)
    return output
