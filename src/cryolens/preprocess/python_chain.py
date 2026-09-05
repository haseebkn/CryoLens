"""Pure-Python SAR radiometric calibration, denoising, and geocoding engine."""

import logging
from typing import Any

import numpy as np
from numpy.typing import NDArray
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import calculate_default_transform, reproject

from cryolens.config.settings import get_app_config
from cryolens.preprocess.s1denoise import S1SubswathDenoise

logger = logging.getLogger(__name__)


class PurePythonSARProcessor:
    """Lightweight pure-Python Sentinel-1 radiometric calibration and geocoding chain."""

    def __init__(self, target_crs: str = "EPSG:3978", pixel_spacing_m: float = 40.0) -> None:
        app_config = get_app_config()
        self.target_crs = target_crs or app_config.project.spatial.target_crs
        self.pixel_spacing = pixel_spacing_m or app_config.project.spatial.pixel_spacing_m
        self.denoiser = S1SubswathDenoise()

    def calibrate_dn_to_sigma0(
        self,
        dn_array: NDArray[np.floating],
        calibration_lut: NDArray[np.floating] | float,
    ) -> NDArray[np.floating]:
        """Convert raw SAR digital numbers (DN) to linear Sigma Nought (sigma0 = DN^2 / A_i^2)."""
        safe_dn = np.asarray(dn_array, dtype=np.float64)
        lut = np.asarray(calibration_lut, dtype=np.float64)
        if np.any(~np.isfinite(lut)) or np.any(lut <= 0):
            raise ValueError("Calibration LUT must contain finite positive values.")
        safe_dn = np.where(np.isfinite(safe_dn) & (safe_dn > 0), safe_dn, np.nan)
        sigma0_linear = (safe_dn**2) / (lut**2)
        return np.asarray(sigma0_linear, dtype=np.float32)

    def generate_incidence_angle_grid(
        self,
        height: int,
        width: int,
        near_angle_deg: float = 19.0,
        far_angle_deg: float = 47.0,
    ) -> NDArray[np.floating]:
        """Generate range-varying incidence angle grid for S1 EW mode (19 to 47 degrees)."""
        angles_1d = np.linspace(near_angle_deg, far_angle_deg, width, dtype=np.float32)
        grid = np.tile(angles_1d, (height, 1))
        return np.asarray(grid, dtype=np.float32)

    def process_calibrated_arrays(
        self,
        sigma0_hh_linear: NDArray[np.floating],
        sigma0_hv_linear: NDArray[np.floating],
        incidence_angle_deg: NDArray[np.floating],
        latitude: NDArray[np.floating],
        longitude: NDArray[np.floating],
        apply_denoise: bool = False,
        gcp_step: int = 64,
    ) -> dict[str, Any]:
        """Build the 4-band stack from already-calibrated sigma-nought.

        This is the path taken for real SAFE products, where calibration and
        thermal noise removal have already been applied by
        :class:`~cryolens.preprocess.safe_reader.SAFEProductReader`. Geocoding
        uses ground control points sampled from the product geolocation grid
        rather than a bounding-box affine, because a Sentinel-1 swath is not a
        north-up rectangle and treating it as one displaces targets by
        kilometres at the swath edges.

        Args:
            gcp_step: Sampling stride, in pixels, for ground control points.
        """
        from rasterio.control import GroundControlPoint

        logger.info(
            "Building 4-band stack from calibrated sigma0 (shape %s)", sigma0_hh_linear.shape
        )

        if apply_denoise:
            raise ValueError(
                "Calibrated SAFE arrays are already noise corrected; do not denoise twice."
            )
        arrays = [sigma0_hh_linear, sigma0_hv_linear, incidence_angle_deg, latitude, longitude]
        if sigma0_hh_linear.ndim != 2 or any(a.shape != sigma0_hh_linear.shape for a in arrays):
            raise ValueError("All calibrated and geolocation arrays must share a 2-D shape.")
        if gcp_step < 1 or min(sigma0_hh_linear.shape) < 2:
            raise ValueError("Geocoding requires a positive GCP stride and at least a 2x2 raster.")
        if not np.isfinite(latitude).all() or not np.isfinite(longitude).all():
            raise ValueError("Geolocation must be finite across the scene.")
        if np.any(np.abs(latitude) > 90) or np.any(np.abs(longitude) > 180):
            raise ValueError("Geolocation is outside WGS84 coordinate limits.")
        hv_linear = np.asarray(sigma0_hv_linear, dtype=np.float32)
        valid = (
            np.isfinite(sigma0_hh_linear)
            & (sigma0_hh_linear > 0)
            & np.isfinite(hv_linear)
            & (hv_linear > 0)
            & np.isfinite(incidence_angle_deg)
            & (incidence_angle_deg > 0)
            & (incidence_angle_deg < 90)
        )

        floor_linear = 1e-5  # -50 dB
        sigma0_hh_db = 10.0 * np.log10(np.maximum(sigma0_hh_linear, floor_linear))
        sigma0_hv_db = 10.0 * np.log10(np.maximum(hv_linear, floor_linear))
        ratio_hh_hv = sigma0_hh_db - sigma0_hv_db

        h, w = sigma0_hh_db.shape

        gcps = []
        for r in sorted(set(range(0, h, gcp_step)) | {h - 1}):
            for c in sorted(set(range(0, w, gcp_step)) | {w - 1}):
                gcps.append(
                    GroundControlPoint(
                        row=float(r) + 0.5,
                        col=float(c) + 0.5,
                        x=float(longitude[r, c]),
                        y=float(latitude[r, c]),
                        z=0.0,
                    )
                )
        if len(gcps) < 3:
            raise ValueError("Too few ground control points to geocode the scene.")
        logger.info("Geocoding from %d ground control points", len(gcps))

        src_crs = "EPSG:4326"

        dst_transform, dst_w, dst_h = calculate_default_transform(
            src_crs,
            self.target_crs,
            w,
            h,
            gcps=gcps,
            resolution=self.pixel_spacing,
        )

        raw_stack = {
            "sigma0_hh_db": np.asarray(sigma0_hh_db, dtype=np.float32),
            "sigma0_hv_db": np.asarray(sigma0_hv_db, dtype=np.float32),
            "ratio_hh_hv": np.asarray(ratio_hh_hv, dtype=np.float32),
            "incidence_angle": np.asarray(incidence_angle_deg, dtype=np.float32),
        }

        nodata_val = -9999.0
        reprojected_bands: dict[str, NDArray[np.floating]] = {}
        for band_name, band_arr in raw_stack.items():
            dst_arr = np.full((dst_h, dst_w), nodata_val, dtype=np.float32)
            reproject(
                source=np.where(valid, band_arr, nodata_val).astype(np.float32),
                destination=dst_arr,
                src_crs=src_crs,
                gcps=gcps,
                dst_transform=dst_transform,
                dst_crs=self.target_crs,
                resampling=Resampling.bilinear,
                src_nodata=nodata_val,
                dst_nodata=nodata_val,
            )
            reprojected_bands[band_name] = dst_arr

        return {
            "bands": reprojected_bands,
            "transform": dst_transform,
            "crs": self.target_crs,
            "shape": (dst_h, dst_w),
            "nodata": nodata_val,
            "geolocation_method": "annotation_gcps",
            "orbit_correction_applied": False,
            "terrain_correction_applied": False,
        }

    def process_scene_arrays(
        self,
        hh_dn: NDArray[np.floating],
        hv_dn: NDArray[np.floating],
        source_bounds: tuple[float, float, float, float],
        source_crs: str = "EPSG:4326",
        calibration_lut_hh: NDArray[np.floating] | float | None = None,
        calibration_lut_hv: NDArray[np.floating] | float | None = None,
        apply_denoise: bool = False,
        noise_equivalent_sigma0_hv: NDArray[np.floating] | None = None,
    ) -> dict[str, Any]:
        """Process explicitly calibrated north-up test arrays; SAFE scenes require GCPs.

        Incidence is a nominal ramp for this synthetic/affine helper only.
        """
        if calibration_lut_hh is None or calibration_lut_hv is None:
            raise ValueError(
                "Explicit calibration LUTs are required; unity is not a sensor calibration."
            )
        if hh_dn.ndim != 2 or hh_dn.shape != hv_dn.shape:
            raise ValueError("HH and HV must share a 2-D shape.")
        logger.info("Calibrating HH and HV DN arrays (shape: %s)...", hh_dn.shape)

        # 1. Radiometric calibration to linear power
        sigma0_hh_linear = self.calibrate_dn_to_sigma0(hh_dn, calibration_lut=calibration_lut_hh)
        sigma0_hv_linear = self.calibrate_dn_to_sigma0(hv_dn, calibration_lut=calibration_lut_hv)

        # 2. Subswath cross-pol thermal noise removal (s1denoise)
        if apply_denoise:
            logger.info("Applying s1denoise inter-subswath thermal noise balancing to HV band...")
            sigma0_hv_linear, _ = self.denoiser.denoise(
                sigma0_hv_linear, noise_equivalent_sigma0_hv
            )

        # 3. Convert to Decibels
        # Clamp to avoid log(0)
        floor_linear = 1e-5  # -50 dB
        sigma0_hh_db = 10.0 * np.log10(np.maximum(sigma0_hh_linear, floor_linear))
        sigma0_hv_db = 10.0 * np.log10(np.maximum(sigma0_hv_linear, floor_linear))

        # 4. Polarimetric ratio (dB difference)
        ratio_hh_hv = sigma0_hh_db - sigma0_hv_db

        # 5. Incidence angle
        h, w = hh_dn.shape
        inc_angle = self.generate_incidence_angle_grid(h, w)

        # 6. Reproject all 4 bands to target CRS (EPSG:3978)
        w_min, s_min, e_max, n_max = source_bounds
        src_transform = from_bounds(w_min, s_min, e_max, n_max, w, h)

        dst_transform, dst_w, dst_h = calculate_default_transform(
            source_crs,
            self.target_crs,
            w,
            h,
            left=w_min,
            bottom=s_min,
            right=e_max,
            top=n_max,
            resolution=self.pixel_spacing,
        )

        reprojected_bands: dict[str, NDArray[np.floating]] = {}
        raw_stack = {
            "sigma0_hh_db": sigma0_hh_db,
            "sigma0_hv_db": sigma0_hv_db,
            "ratio_hh_hv": ratio_hh_hv,
            "incidence_angle": inc_angle,
        }

        nodata_val = -9999.0
        for band_name, band_arr in raw_stack.items():
            dst_arr = np.full((dst_h, dst_w), nodata_val, dtype=np.float32)
            reproject(
                source=band_arr,
                destination=dst_arr,
                src_transform=src_transform,
                src_crs=source_crs,
                dst_transform=dst_transform,
                dst_crs=self.target_crs,
                resampling=Resampling.bilinear,
                src_nodata=nodata_val,
                dst_nodata=nodata_val,
            )
            reprojected_bands[band_name] = dst_arr

        return {
            "bands": reprojected_bands,
            "transform": dst_transform,
            "crs": self.target_crs,
            "shape": (dst_h, dst_w),
            "nodata": nodata_val,
        }
