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
        calibration_lut: NDArray[np.floating] | float = 1.0,
    ) -> NDArray[np.floating]:
        """Convert raw SAR digital numbers (DN) to linear Sigma Nought (sigma0 = DN^2 / A_i^2)."""
        # Ensure positive non-zero values
        safe_dn = np.maximum(dn_array, 0.0)
        lut = np.maximum(calibration_lut, 1e-6)
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
        apply_denoise: bool = True,
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
        from rasterio.transform import from_gcps

        logger.info(
            "Building 4-band stack from calibrated sigma0 (shape %s)", sigma0_hh_linear.shape
        )

        hv_linear = np.asarray(sigma0_hv_linear, dtype=np.float32)
        if apply_denoise:
            logger.info("Applying s1denoise inter-subswath balancing to the HV channel...")
            hv_linear, _ = self.denoiser.denoise(hv_linear)

        floor_linear = 1e-5  # -50 dB
        sigma0_hh_db = 10.0 * np.log10(np.maximum(sigma0_hh_linear, floor_linear))
        sigma0_hv_db = 10.0 * np.log10(np.maximum(hv_linear, floor_linear))
        ratio_hh_hv = sigma0_hh_db - sigma0_hv_db

        h, w = sigma0_hh_db.shape

        gcps = []
        for r in range(0, h, gcp_step):
            for c in range(0, w, gcp_step):
                gcps.append(
                    GroundControlPoint(
                        row=float(r),
                        col=float(c),
                        x=float(longitude[r, c]),
                        y=float(latitude[r, c]),
                    )
                )
        if len(gcps) < 3:
            raise ValueError("Too few ground control points to geocode the scene.")
        logger.info("Geocoding from %d ground control points", len(gcps))

        src_transform = from_gcps(gcps)
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
                source=band_arr,
                destination=dst_arr,
                src_transform=src_transform,
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
        }

    def process_scene_arrays(
        self,
        hh_dn: NDArray[np.floating],
        hv_dn: NDArray[np.floating],
        source_bounds: tuple[float, float, float, float],
        source_crs: str = "EPSG:4326",
        calibration_lut_hh: NDArray[np.floating] | float = 1.0,
        calibration_lut_hv: NDArray[np.floating] | float = 1.0,
        apply_denoise: bool = True,
    ) -> dict[str, Any]:
        """Execute calibration, cross-pol subswath denoising, and reproject to target CRS."""
        logger.info("Calibrating HH and HV DN arrays (shape: %s)...", hh_dn.shape)

        # 1. Radiometric calibration to linear power
        sigma0_hh_linear = self.calibrate_dn_to_sigma0(hh_dn, calibration_lut=calibration_lut_hh)
        sigma0_hv_linear = self.calibrate_dn_to_sigma0(hv_dn, calibration_lut=calibration_lut_hv)

        # 2. Subswath cross-pol thermal noise removal (s1denoise)
        if apply_denoise:
            logger.info("Applying s1denoise inter-subswath thermal noise balancing to HV band...")
            sigma0_hv_linear, _ = self.denoiser.denoise(sigma0_hv_linear)

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
