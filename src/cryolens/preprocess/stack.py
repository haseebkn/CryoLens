"""4-Band Cloud Optimized GeoTIFF (COG) generator with rio-cogeo validation."""

import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from numpy.typing import NDArray
from rio_cogeo.cogeo import cog_translate, cog_validate
from rio_cogeo.profiles import cog_profiles

logger = logging.getLogger(__name__)

BAND_NAMES = [
    "sigma0_hh_db",
    "sigma0_hv_db",
    "ratio_hh_hv",
    "incidence_angle",
]


class COGStackBuilder:
    """Creates validated 4-band Cloud Optimized GeoTIFF (COG) stacks in EPSG:3978."""

    def __init__(self, output_dir: Path | str = "./data/processed") -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build_and_export_cog(
        self,
        scene_id: str,
        bands: dict[str, NDArray[np.floating]],
        transform: Any,
        crs: str = "EPSG:3978",
        nodata: float = -9999.0,
    ) -> Path:
        """Write calibrated bands to intermediate GeoTIFF and convert to validated COG."""
        target_scene_dir = self.output_dir / scene_id
        target_scene_dir.mkdir(parents=True, exist_ok=True)

        interim_tif = target_scene_dir / f"{scene_id}_interim.tif"
        final_cog = target_scene_dir / f"{scene_id}_4band_EPSG3978.tif"

        # Validate band presence
        for name in BAND_NAMES:
            if name not in bands:
                raise ValueError(f"Missing required band: {name}. Available: {list(bands.keys())}")

        h, w = bands[BAND_NAMES[0]].shape

        logger.info(
            "Writing interim 4-band raster for scene %s (size: %dx%d, CRS: %s)...",
            scene_id,
            w,
            h,
            crs,
        )

        profile = {
            "driver": "GTiff",
            "height": h,
            "width": w,
            "count": 4,
            "dtype": "float32",
            "crs": crs,
            "transform": transform,
            "nodata": nodata,
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
        }

        with rasterio.open(interim_tif, "w", **profile) as dst:
            for idx, name in enumerate(BAND_NAMES, start=1):
                dst.write(bands[name].astype(np.float32), idx)
                dst.set_band_description(idx, name)

        logger.info("Converting %s to Cloud Optimized GeoTIFF (COG)...", interim_tif.name)
        dst_profile = cog_profiles.get("deflate")
        cog_translate(
            interim_tif,
            final_cog,
            dst_profile,
            overview_level=4,
            in_memory=True,
            quiet=True,
        )

        # Cleanup interim file
        if interim_tif.exists():
            interim_tif.unlink()

        # Validate COG compliance
        is_valid, errors, warnings = cog_validate(str(final_cog))
        if not is_valid:
            raise ValueError(f"Generated COG failed validation: {errors}")

        if warnings:
            logger.warning("COG validation warnings: %s", warnings)

        logger.info(
            "Successfully produced validated COG: %s (%.2f MB)",
            final_cog,
            final_cog.stat().st_size / (1024**2),
        )
        return final_cog
