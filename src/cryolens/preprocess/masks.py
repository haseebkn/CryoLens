"""Land masking from GSHHG shorelines and sea ice margin masking.

Coastal returns are the largest single source of false alarms in maritime SAR
detection: land is bright in both polarisations, the shoreline is geometrically
complex at Sentinel-1 EW resolution, and any land left inside a CFAR training
window inflates the local clutter mean and suppresses genuine targets nearby.

Two things therefore matter here and both are implemented rather than
approximated:

1. **A real shoreline.** GSHHG (Global Self-consistent, Hierarchical,
   High-resolution Geography) full-resolution level-1 polygons are used, not a
   hand-drawn outline. Newfoundland and Labrador have an intricate coast with
   thousands of islands, fjords and skerries; a coarse outline leaves bright
   land inside the analysis mask and produces exactly the false alarms this
   project is trying to remove.

2. **A dilation buffer.** Masking only the land polygon is not enough, because
   geolocation error, layover, and the CFAR background window all reach beyond
   the coastline. The mask is buffered seaward by a configurable distance.

The clipped regional subset is cached as a GeoPackage on first use, because the
full-resolution GSHHG shapefile is 161 MB and reading it per scene would
dominate runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from rasterio.features import rasterize
from shapely.geometry import Polygon, box, mapping

logger = logging.getLogger(__name__)

# Newfoundland & Labrador marine area, matching configs/aoi.geojson. Used to
# clip the global shoreline down to a regional cache.
NL_AOI_BBOX = (-64.5, 42.5, -44.0, 60.5)

DEFAULT_GSHHG_ROOT = Path("./data/cache/gshhg/GSHHS_shp")
DEFAULT_CACHE_PATH = Path("./data/cache/gshhg/nl_shoreline.gpkg")


class LandMaskGenerator:
    """Rasterises GSHHG shorelines into binary land masks with a coastal buffer."""

    def __init__(
        self,
        gshhg_root: Path | str = DEFAULT_GSHHG_ROOT,
        resolution: str = "f",
        cache_path: Path | str = DEFAULT_CACHE_PATH,
        aoi_bbox: tuple[float, float, float, float] = NL_AOI_BBOX,
        coastal_buffer_m: float = 500.0,
        allow_custom_only: bool = False,
    ) -> None:
        """Configure the shoreline source and buffer.

        Args:
            gshhg_root: Directory containing the extracted ``GSHHS_shp`` tree.
            resolution: GSHHG resolution code, ``f`` (full) through ``c`` (crude).
            cache_path: Where the clipped regional shoreline is cached.
            aoi_bbox: Clip window as (west, south, east, north) in WGS84.
            coastal_buffer_m: Seaward dilation applied to every land polygon.
        """
        self.gshhg_root = Path(gshhg_root)
        self.resolution = resolution
        self.cache_path = Path(cache_path)
        self.aoi_bbox = aoi_bbox
        self.coastal_buffer_m = coastal_buffer_m
        self.allow_custom_only = allow_custom_only
        self._geometries: list[Any] | None = None
        self._fallback_polygons: list[Polygon] = []

    def add_custom_polygon(self, polygon: Polygon) -> None:
        """Add an extra land polygon, for example a fixed offshore structure."""
        self._fallback_polygons.append(polygon)
        self._geometries = None

    def _shapefile_path(self, level: int = 1) -> Path:
        """Path to the GSHHG shapefile for the configured resolution and level."""
        return self.gshhg_root / self.resolution / f"GSHHS_{self.resolution}_L{level}.shp"

    def _build_regional_cache(self) -> None:
        """Clip the global shoreline to the AOI and cache it as a GeoPackage."""
        import geopandas as gpd

        src = self._shapefile_path(level=1)
        if not src.is_file():
            raise FileNotFoundError(
                f"GSHHG shoreline not found at {src}. Download and extract "
                "gshhg-shp-2.3.7.zip into data/cache/gshhg/ (see make fetch-shorelines)."
            )

        logger.info("Clipping %s to the AOI; this runs once and is then cached.", src.name)
        window = box(*self.aoi_bbox)
        gdf = gpd.read_file(src, bbox=self.aoi_bbox)
        if gdf.empty:
            raise ValueError(f"No shoreline polygons intersect {self.aoi_bbox}.")

        gdf = gdf[gdf.geometry.notna()]
        gdf["geometry"] = gdf.geometry.intersection(window)
        gdf = gdf[~gdf.geometry.is_empty]

        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(self.cache_path, driver="GPKG")
        logger.info("Cached %d regional shoreline polygons to %s", len(gdf), self.cache_path)

    def load_geometries(self) -> list[Any]:
        """Load the regional shoreline, building the cache if necessary."""
        if self._geometries is not None:
            return self._geometries

        import geopandas as gpd

        try:
            if not self.cache_path.is_file():
                self._build_regional_cache()
            gdf = gpd.read_file(self.cache_path)
            if gdf.crs is None:
                raise ValueError("Shoreline cache is missing its CRS.")
            gdf = gdf.to_crs("EPSG:4326")
            geoms = list(gdf.geometry)
            logger.info("Loaded %d shoreline polygons from %s", len(geoms), self.cache_path.name)
        except (FileNotFoundError, ValueError) as exc:
            if not self._fallback_polygons or not self.allow_custom_only:
                raise
            logger.warning("Falling back to custom polygons only: %s", exc)
            geoms = []

        geoms.extend(self._fallback_polygons)
        if not geoms or any(g is None or g.is_empty or not g.is_valid for g in geoms):
            raise ValueError(
                "Shoreline geometry must be nonempty and valid; refusing an all-water fallback."
            )
        self._geometries = geoms
        return geoms

    def generate_land_mask(
        self,
        shape: tuple[int, int],
        transform: Any,
        crs: str = "EPSG:3978",
        buffer_m: float | None = None,
    ) -> NDArray[np.bool_]:
        """Rasterise land onto the target grid. True is land, False is water.

        Args:
            shape: Output raster shape as (height, width).
            transform: Affine transform of the output grid.
            crs: CRS of the output grid; polygons are reprojected into it.
            buffer_m: Override for the coastal dilation distance, in metres of
                the target CRS. Uses the instance default when omitted.
        """
        import pyproj
        from shapely.ops import transform as shapely_transform

        geoms = self.load_geometries()
        if not geoms:
            raise ValueError("No shoreline geometry available; refusing an all-water mask.")

        project = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
        dilation = self.coastal_buffer_m if buffer_m is None else buffer_m
        if not np.isfinite(dilation) or dilation < 0:
            raise ValueError("Coastal buffer must be finite and nonnegative.")
        target_crs = pyproj.CRS(crs)
        if dilation > 0 and (
            not target_crs.is_projected
            or any(abs(axis.unit_conversion_factor - 1.0) > 1e-9 for axis in target_crs.axis_info)
        ):
            raise ValueError("Coastal buffering requires a projected CRS measured in metres.")

        shapes: list[dict[str, Any]] = []
        for poly in geoms:
            try:
                projected = shapely_transform(project, poly)
                if projected.is_empty:
                    continue
                if dilation > 0.0:
                    projected = projected.buffer(dilation)
                shapes.append(mapping(projected))
            except Exception as exc:
                raise ValueError(
                    "Cannot project a shoreline polygon; mask coverage is incomplete."
                ) from exc

        if not shapes:
            raise ValueError("No projectable shoreline geometry; refusing an all-water mask.")

        mask_arr = rasterize(
            shapes=shapes,
            out_shape=shape,
            transform=transform,
            fill=0,
            default_value=1,
            dtype=np.uint8,
            all_touched=True,
        )
        mask = np.asarray(mask_arr == 1, dtype=np.bool_)
        logger.info(
            "Land mask covers %.2f%% of the grid (buffer %.0f m)",
            100.0 * mask.mean(),
            dilation,
        )
        return mask


class SeaIceMaskGenerator:
    """Derive conservative screening exclusion masks for ice and unknown cover.

    True means excluded, including missing/invalid context. It must not be used
    to report measured sea-ice extent or concentration.
    """

    ICE_EDGE_CONCENTRATION = 0.15
    """Conventional ice-edge definition used operationally by the CIS."""

    def __init__(self, concentration_threshold: float = ICE_EDGE_CONCENTRATION) -> None:
        """Configure the ice-edge threshold as a fraction between 0 and 1."""
        if not 0.0 <= concentration_threshold <= 1.0:
            raise ValueError("concentration_threshold must lie in [0, 1].")
        self.concentration_threshold = concentration_threshold

    def from_concentration(
        self,
        concentration: NDArray[np.floating],
    ) -> NDArray[np.bool_]:
        """Threshold a concentration field given as a fraction in [0, 1]."""
        values = np.asarray(concentration)
        return np.asarray(
            ~np.isfinite(values)
            | (values < 0)
            | (values > 1)
            | (values >= self.concentration_threshold),
            dtype=np.bool_,
        )

    def from_sic_class(
        self,
        sic_class: NDArray[np.integer],
        fill_value: int = 255,
    ) -> NDArray[np.bool_]:
        """Threshold a class-encoded concentration map in 10 percent steps.

        Class 2 (20 percent) is the first bin strictly above the 15 percent ice
        edge, so it is the lowest class treated as ice.
        """
        min_class = int(np.ceil(self.concentration_threshold * 10.0))
        return np.asarray(
            (sic_class >= max(min_class, 1)) | (sic_class == fill_value) | (sic_class < 0),
            dtype=np.bool_,
        )

    def generate_ice_mask(
        self,
        shape: tuple[int, int],
        concentration: NDArray[np.floating] | None = None,
        default_ice_fraction: float | None = None,
    ) -> NDArray[np.bool_]:
        """Generate an exclusion mask; uniform synthetic fields require explicit opt-in."""
        if concentration is not None:
            if concentration.shape != shape:
                raise ValueError("Ice concentration must match the requested raster shape.")
            return self.from_concentration(concentration)

        if default_ice_fraction is None:
            raise ValueError(
                "Measured ice concentration is required; unknown ice cover is not open water."
            )

        logger.warning(
            "No ice concentration supplied; assuming a uniform %.0f%% field. "
            "Ice-stratified metrics from this run are not meaningful.",
            default_ice_fraction * 100.0,
        )
        field = np.full(shape, default_ice_fraction, dtype=np.float32)
        return self.from_concentration(field)
