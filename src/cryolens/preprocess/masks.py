"""Land masking (GSHHG/OSM) and sea ice margin context masking."""

import logging
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from rasterio.features import rasterize
from shapely.geometry import Polygon, mapping

logger = logging.getLogger(__name__)

# Approximate Newfoundland & Labrador coastal landmass polygons in WGS84 for offline/default use
NEWFOUNDLAND_AVALON_POLY = Polygon(
    [
        [-54.0, 46.5],
        [-52.5, 46.5],
        [-52.5, 48.2],
        [-53.5, 48.2],
        [-54.0, 47.5],
        [-54.0, 46.5],
    ]
)
NEWFOUNDLAND_MAIN_POLY = Polygon(
    [
        [-59.5, 47.5],
        [-54.0, 47.5],
        [-53.0, 49.5],
        [-55.5, 51.7],
        [-57.2, 51.5],
        [-59.5, 49.0],
        [-59.5, 47.5],
    ]
)


class LandMaskGenerator:
    """Generates binary land masks to filter out coastal false alarms."""

    def __init__(self, cache_dir: Path | str = "./data/cache/gshhg") -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.polygons = [NEWFOUNDLAND_AVALON_POLY, NEWFOUNDLAND_MAIN_POLY]

    def add_custom_polygon(self, polygon: Polygon) -> None:
        """Add custom coastline/island polygon to mask definition."""
        self.polygons.append(polygon)

    def generate_land_mask(
        self,
        shape: tuple[int, int],
        transform: Any,
        crs: str = "EPSG:3978",
    ) -> NDArray[np.bool_]:
        """Rasterize land polygons onto the target grid (True = Land, False = Ocean)."""
        import pyproj
        from shapely.ops import transform as shapely_transform

        h, w = shape

        # Reproject WGS84 polygons to target CRS (e.g. EPSG:3978)
        project_func = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
        reprojected_geoms: list[dict[str, Any]] = []

        for poly in self.polygons:
            try:
                reprojected_poly = shapely_transform(project_func, poly)
                if not reprojected_poly.is_empty:
                    reprojected_geoms.append(mapping(reprojected_poly))
            except Exception as e:
                logger.warning("Failed to reproject land polygon: %s", e)

        if not reprojected_geoms:
            return np.zeros(shape, dtype=np.bool_)

        mask_arr = rasterize(
            shapes=reprojected_geoms,
            out_shape=(h, w),
            transform=transform,
            fill=0,
            default_value=1,
            dtype=np.uint8,
        )

        return np.asarray(mask_arr == 1, dtype=np.bool_)


class SeaIceMaskGenerator:
    """Generates sea ice concentration / ice edge condition mask."""

    def __init__(self) -> None:
        pass

    def generate_ice_mask(
        self,
        shape: tuple[int, int],
        concentration_threshold: float = 0.15,
        default_ice_fraction: float = 0.0,
    ) -> NDArray[np.bool_]:
        """Generate binary ice mask (True = Sea Ice / Pack Ice, False = Open Water)."""
        # Default fallback: uniform sea ice regime array
        ice_field = np.full(shape, default_ice_fraction, dtype=np.float32)
        return np.asarray(ice_field >= concentration_threshold, dtype=np.bool_)
