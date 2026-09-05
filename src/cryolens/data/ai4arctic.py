"""Reader for the AI4Arctic Sea Ice Challenge "ready-to-train" dataset.

The ready-to-train NetCDFs contain real Sentinel-1 EW GRDM acquisitions that have
already passed through NERSC thermal-noise correction, co-registered CIS/DMI ice
charts, ERA5 forcing, and a land-distance zonation. That makes them the only
source in this project that provides *real* dual-pol SAR together with the ice
and land context needed to measure false-alarm behaviour honestly.

Two properties of the distribution matter and are handled here:

1. **The pixel values are standardised, not physical.** The publisher's
   global mean/std coefficients are inverted, never the observed scene extrema.
   The min/max attributes predate downsampling/masking and do not determine
   the standardisation of a cropped or resampled scene.

2. **The geolocation is a coarse 21x21 tie-point grid**, not an affine
   transform. It is bilinearly interpolated up to full raster size.

Reference: AI4Arctic Sea Ice Challenge Dataset, DTU, DOI 10.11583/DTU.c.6244065.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

# The land-distance variable is a zonation with integer ids 0..41, where 0 is
# land (or the innermost coastal zone) and larger ids are progressively further
# offshore. Documented in the variable's own ``long_name``.
LAND_DISTANCE_ZONE_MAX = 41
LAND_ZONE_ID = 0

# Sea ice concentration is class-encoded in 11 steps of 10 percent.
SIC_CLASS_TO_PERCENT = 10.0
SIC_FILL_VALUE = 255

_SAR_PRIMARY = "nersc_sar_primary"
_SAR_SECONDARY = "nersc_sar_secondary"

# Publisher toolkit misc/global_meanstd.npy, retrieved 2026-09-05:
# https://github.com/astokholm/AI4ArcticSeaIceChallenge/blob/main/misc/global_meanstd.npy
# SHA256: 2f3f9188617a98c9e89158302efad8ffbc22ecc9d66d76019b54cdb5cbcfdeca
# Numeric constants extracted with pickletools without executing its pickle.
PUBLISHER_MEAN_STD = {
    _SAR_PRIMARY: (-14.5082551552228, 5.6597459193818676),
    _SAR_SECONDARY: (-24.701205147144236, 4.746759305258515),
    "distance_map": (23.627669500714102, 14.99018630794473),
    "sar_incidenceangle": (33.99801788456536, 8.322267212079046),
    "u10m_rotated": (0.6747986162545088, 4.749646758314658),
    "v10m_rotated": (0.6075565949388854, 5.196104947581429),
}


def _restore_standardised(values: NDArray[np.floating], variable: str) -> NDArray[np.float32]:
    """Invert the published global standard scaler without scene-dependent fitting."""
    mean, std = PUBLISHER_MEAN_STD[variable]
    return np.asarray(np.where(np.isfinite(values), values * std + mean, np.nan), dtype=np.float32)


@dataclass(frozen=True)
class SceneExtent:
    """Geographic bounding box of a scene, used for cheap AOI pre-filtering."""

    path: Path
    scene_id: str
    original_id: str
    ice_service: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def intersects(self, bbox: tuple[float, float, float, float]) -> bool:
        """Return True if this extent overlaps ``bbox`` given as (W, S, E, N)."""
        west, south, east, north = bbox
        if self.lat_max < south or self.lat_min > north:
            return False
        if self.lon_max < west or self.lon_min > east:
            return False
        return True

    @property
    def centre(self) -> tuple[float, float]:
        """Approximate scene centre as (longitude, latitude)."""
        return ((self.lon_min + self.lon_max) / 2.0, (self.lat_min + self.lat_max) / 2.0)

    def centre_within(self, bbox: tuple[float, float, float, float]) -> bool:
        """Return True if the scene *centre* falls inside ``bbox``.

        Stricter than :meth:`intersects`. A Sentinel-1 EW swath is roughly 400 km
        across, so a scene can clip the corner of the area of interest while
        lying almost entirely outside it â€” for example an Ungava Bay acquisition
        touching the western edge of the Labrador box. Centre containment keeps
        the scene set genuinely regional.
        """
        west, south, east, north = bbox
        lon, lat = self.centre
        return west <= lon <= east and south <= lat <= north

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dictionary."""
        return {
            "path": str(self.path).replace("\\", "/"),
            "scene_id": self.scene_id,
            "original_id": self.original_id,
            "ice_service": self.ice_service,
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
        }


@dataclass
class AI4ArcticScene:
    """A single AI4Arctic scene restored to physical units where possible."""

    scene_id: str
    original_id: str
    ice_service: str
    pixel_spacing_m: float

    sigma0_hh_db: NDArray[np.float32]
    sigma0_hv_db: NDArray[np.float32]
    incidence_angle_deg: NDArray[np.float32]
    land_distance_zone: NDArray[np.int16]

    latitude: NDArray[np.float32]
    longitude: NDArray[np.float32]

    sic_class: NDArray[np.uint8] | None = None
    sod_class: NDArray[np.uint8] | None = None
    floe_class: NDArray[np.uint8] | None = None
    wind_speed_normalised: NDArray[np.float32] | None = None
    wind_speed_m_s: NDArray[np.float32] | None = None

    assumptions: dict[str, str] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int]:
        """Raster shape as (lines, samples)."""
        return (int(self.sigma0_hh_db.shape[0]), int(self.sigma0_hh_db.shape[1]))

    @property
    def valid_mask(self) -> NDArray[np.bool_]:
        """Pixels with finite backscatter in both polarisations."""
        return (
            np.isfinite(self.sigma0_hh_db)
            & np.isfinite(self.sigma0_hv_db)
            & np.isfinite(self.latitude)
            & np.isfinite(self.longitude)
            & np.isfinite(self.incidence_angle_deg)
            & (self.incidence_angle_deg > 0)
            & (self.incidence_angle_deg < 90)
        )

    @property
    def land_mask(self) -> NDArray[np.bool_]:
        """True where the land-distance zonation marks land."""
        return self.land_distance_zone <= LAND_ZONE_ID

    def sea_ice_fraction(self) -> float | None:
        """Fraction of charted pixels with sea ice concentration above the ice edge.

        Returns ``None`` â€” not zero â€” when the scene carries no usable ice chart.
        The AI4Arctic *challenge test* scenes have their SIC/SOD/FLOE labels
        withheld (every pixel is the 255 fill value), and the withheld truth
        ships separately in the matching ``*_prep_reference.nc`` file. Returning
        0.0 for those scenes would silently classify them as open water and
        corrupt any ice-stratified metric, so the absence of a chart is
        represented explicitly.
        """
        if self.sic_class is None:
            return None
        charted = self.sic_class <= 10
        if not charted.any():
            return None
        ice = (self.sic_class >= 2) & charted  # class 2 == 20 percent, first bin above 15
        return float(ice.sum() / charted.sum())

    @property
    def has_ice_chart(self) -> bool:
        """True when the scene carries a usable sea ice chart."""
        return self.sea_ice_fraction() is not None

    def pixel_area_km2(self) -> float:
        """Ground area of one pixel in square kilometres."""
        return (self.pixel_spacing_m / 1000.0) ** 2


def _interpolate_tiepoint_grid(
    grid: NDArray[np.floating],
    shape: tuple[int, int],
) -> NDArray[np.float32]:
    """Bilinearly expand a coarse tie-point grid to full raster ``shape``.

    The first grid axis maps to image lines (azimuth) and the second to image
    samples (range); this orientation was verified against the recorded scene
    extents rather than assumed from the NetCDF dimension names, which are
    transposed relative to the raster.
    """
    lines, samples = shape
    g_rows, g_cols = grid.shape

    row_src = np.linspace(0.0, g_rows - 1.0, lines, dtype=np.float64)
    col_src = np.linspace(0.0, g_cols - 1.0, samples, dtype=np.float64)

    r0 = np.clip(np.floor(row_src).astype(np.intp), 0, g_rows - 1)
    r1 = np.clip(r0 + 1, 0, g_rows - 1)
    c0 = np.clip(np.floor(col_src).astype(np.intp), 0, g_cols - 1)
    c1 = np.clip(c0 + 1, 0, g_cols - 1)

    wr = (row_src - r0)[:, None]
    wc = (col_src - c0)[None, :]

    top = grid[r0][:, c0] * (1.0 - wc) + grid[r0][:, c1] * wc
    bottom = grid[r1][:, c0] * (1.0 - wc) + grid[r1][:, c1] * wc
    return np.asarray(top * (1.0 - wr) + bottom * wr, dtype=np.float32)


def _read_variable(dataset: Any, name: str) -> NDArray[np.float64] | None:
    """Read a NetCDF variable as float64 with masked values converted to NaN."""
    if name not in dataset.variables:
        return None
    raw = dataset.variables[name][:]
    values = np.ma.filled(raw.astype(np.float64), np.nan)
    fill = getattr(dataset.variables[name], "variable_fill_value", None)
    if fill is not None:
        values[values == float(fill)] = np.nan
    return values


def _physical_extremes(dataset: Any, name: str) -> tuple[float, float] | None:
    """Return the recorded pre-normalisation (min, max) for a variable, if present."""
    if name not in dataset.variables:
        return None
    var = dataset.variables[name]
    attrs = set(var.ncattrs())
    if "min" not in attrs or "max" not in attrs:
        return None
    try:
        return (float(var.min), float(var.max))
    except (TypeError, ValueError):
        return None


def load_scene(path: Path | str, load_context: bool = True) -> AI4ArcticScene:
    """Load one AI4Arctic ready-to-train scene into physical units.

    Args:
        path: Path to a ``*_prep.nc`` file.
        load_context: When False, skip ice chart and wind fields to save memory.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If required physical/geographic metadata are absent or the
            land zonation is inconsistent with the publisher scaler.
    """
    from netCDF4 import Dataset  # imported lazily; heavy binary dependency

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"AI4Arctic scene not found: {path}")

    assumptions: dict[str, str] = {}

    with Dataset(str(path)) as ds:
        scene_id = str(getattr(ds, "scene_id", path.name))
        original_id = str(getattr(ds, "original_id", ""))
        ice_service = str(getattr(ds, "ice_service", ""))
        pixel_spacing = float(getattr(ds, "pixel_spacing", 80.0))

        hh_raw = _read_variable(ds, _SAR_PRIMARY)
        hv_raw = _read_variable(ds, _SAR_SECONDARY)
        if hh_raw is None or hv_raw is None:
            raise ValueError(f"{path.name} is missing NERSC SAR channels.")

        hh_extremes = _physical_extremes(ds, _SAR_PRIMARY)
        hv_extremes = _physical_extremes(ds, _SAR_SECONDARY)
        if hh_extremes is None or hv_extremes is None:
            raise ValueError(
                f"{path.name} lacks min/max attributes on the SAR channels, so "
                "sigma-nought cannot be restored to decibels. Refusing to load "
                "rather than run CFAR on standardised values."
            )

        if hh_raw.ndim != 2 or hh_raw.shape != hv_raw.shape:
            raise ValueError("SAR channels must share a 2-D shape.")
        if (
            str(getattr(ds.variables[_SAR_PRIMARY], "polarisation", "")) != "HH"
            or str(getattr(ds.variables[_SAR_SECONDARY], "polarisation", "")) != "HV"
        ):
            raise ValueError("The screening pipeline requires verified HH and HV polarisations.")
        if not np.isfinite(pixel_spacing) or pixel_spacing <= 0:
            raise ValueError("Pixel spacing must be finite and positive.")
        sigma0_hh_db = _restore_standardised(hh_raw, _SAR_PRIMARY)
        sigma0_hv_db = _restore_standardised(hv_raw, _SAR_SECONDARY)
        assumptions["normalisation"] = (
            "Publisher global_meanstd.npy SHA256 "
            "2f3f9188617a98c9e89158302efad8ffbc22ecc9d66d76019b54cdb5cbcfdeca; "
            "physical = stored * global_std + global_mean."
        )
        shape = (int(sigma0_hh_db.shape[0]), int(sigma0_hh_db.shape[1]))

        inc_raw = _read_variable(ds, "sar_incidenceangle")
        if inc_raw is not None:
            incidence = _restore_standardised(inc_raw, "sar_incidenceangle")
        else:
            raise ValueError(
                "Incidence angle is required; a nominal ramp is not measured geometry."
            )

        dist_raw = _read_variable(ds, "distance_map")
        if dist_raw is not None:
            zones = _restore_standardised(dist_raw, "distance_map")
            finite = np.isfinite(zones)
            if np.any(np.abs(zones[finite] - np.rint(zones[finite])) > 0.01):
                raise ValueError(
                    "Distance zones do not match the published scaler; unsupported dataset version."
                )
            if np.any((zones[finite] < -0.01) | (zones[finite] > 41.01)):
                raise ValueError("Decoded land-distance zones are outside 0..41.")
            land_distance = np.asarray(np.where(finite, np.rint(zones), -1), dtype=np.int16)
        else:
            raise ValueError(
                "Land-distance context is required; unknown coast must not be treated as water."
            )

        if incidence.shape != shape or land_distance.shape != shape:
            raise ValueError("Incidence and land-distance grids must match the SAR raster.")

        lat_grid = _read_variable(ds, "sar_grid2d_latitude")
        lon_grid = _read_variable(ds, "sar_grid2d_longitude")
        if lat_grid is None or lon_grid is None:
            raise ValueError(f"{path.name} is missing its geolocation tie-point grid.")
        if not np.isfinite(lat_grid).all() or not np.isfinite(lon_grid).all():
            raise ValueError("Geolocation tie-points must be finite.")
        if np.any(np.abs(lat_grid) > 90) or np.any(np.abs(lon_grid) > 180):
            raise ValueError("Geolocation tie-points are outside WGS84 coordinate limits.")
        latitude = _interpolate_tiepoint_grid(lat_grid, shape)
        longitude = _interpolate_tiepoint_grid(lon_grid, shape)

        sic = sod = floe = None
        wind_speed = None
        if load_context:
            sic = _read_class_map(ds, "SIC")
            sod = _read_class_map(ds, "SOD")
            floe = _read_class_map(ds, "FLOE")

            u10 = _read_variable(ds, "u10m_rotated")
            v10 = _read_variable(ds, "v10m_rotated")
            if u10 is not None and v10 is not None:
                speed_coarse = np.hypot(
                    _restore_standardised(u10, "u10m_rotated"),
                    _restore_standardised(v10, "v10m_rotated"),
                )
                wind_speed = _interpolate_tiepoint_grid(speed_coarse, shape)
                assumptions["wind_speed"] = (
                    "ERA5 components restored with publisher mean/std before calculating speed in m/s."
                )

    logger.info(
        "Loaded %s (%dx%d @ %.0f m) HH median %.1f dB, HV median %.1f dB",
        scene_id,
        shape[0],
        shape[1],
        pixel_spacing,
        float(np.nanmedian(sigma0_hh_db)),
        float(np.nanmedian(sigma0_hv_db)),
    )

    return AI4ArcticScene(
        scene_id=scene_id,
        original_id=original_id,
        ice_service=ice_service,
        pixel_spacing_m=pixel_spacing,
        sigma0_hh_db=sigma0_hh_db,
        sigma0_hv_db=sigma0_hv_db,
        incidence_angle_deg=incidence,
        land_distance_zone=land_distance,
        latitude=latitude,
        longitude=longitude,
        sic_class=sic,
        sod_class=sod,
        floe_class=floe,
        wind_speed_m_s=wind_speed,
        assumptions=assumptions,
    )


def _read_class_map(dataset: Any, name: str) -> NDArray[np.uint8] | None:
    """Read an ice-chart class map, preserving the 255 fill value."""
    if name not in dataset.variables:
        return None
    raw = dataset.variables[name][:]
    filled = np.ma.filled(raw, SIC_FILL_VALUE)
    return np.asarray(filled, dtype=np.uint8)


def build_scene_index(root: Path | str, output_path: Path | str | None = None) -> list[SceneExtent]:
    """Index every ``*_prep.nc`` under ``root`` by geographic extent.

    Reads only the tie-point grids, so it is cheap enough to run over a whole
    archive. Corrupt or partially downloaded files are logged and skipped rather
    than aborting the index.
    """
    from netCDF4 import Dataset

    root = Path(root)
    extents: list[SceneExtent] = []

    for path in sorted(root.rglob("*_prep.nc")):
        if "_reference" in path.name:
            continue
        try:
            with Dataset(str(path)) as ds:
                lat = np.ma.filled(
                    ds.variables["sar_grid2d_latitude"][:].astype(np.float64), np.nan
                )
                lon = np.ma.filled(
                    ds.variables["sar_grid2d_longitude"][:].astype(np.float64), np.nan
                )
                extents.append(
                    SceneExtent(
                        path=path,
                        scene_id=str(getattr(ds, "scene_id", path.name)),
                        original_id=str(getattr(ds, "original_id", "")),
                        ice_service=str(getattr(ds, "ice_service", "")),
                        lat_min=float(np.nanmin(lat)),
                        lat_max=float(np.nanmax(lat)),
                        lon_min=float(np.nanmin(lon)),
                        lon_max=float(np.nanmax(lon)),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - archive integrity varies
            logger.warning("Skipping unreadable scene %s: %s", path.name, exc)

    logger.info("Indexed %d AI4Arctic scenes under %s", len(extents), root)

    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps([e.to_dict() for e in extents], indent=2), encoding="utf-8"
        )
        logger.info("Wrote scene index to %s", output_path)

    return extents


def scenes_intersecting_aoi(
    extents: list[SceneExtent],
    bbox: tuple[float, float, float, float],
    require_centre: bool = True,
) -> list[SceneExtent]:
    """Filter an index down to scenes within the AOI bounding box (W, S, E, N).

    Args:
        extents: Indexed scene extents.
        bbox: Area of interest as (west, south, east, north).
        require_centre: When True (the default) keep only scenes whose centre
            lies inside the AOI, rather than any scene that merely clips it.
    """
    if require_centre:
        return [e for e in extents if e.centre_within(bbox)]
    return [e for e in extents if e.intersects(bbox)]
