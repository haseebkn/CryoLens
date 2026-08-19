"""Unit tests for 4-band COG stack generation and rio-cogeo validation."""

from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_bounds
from rio_cogeo.cogeo import cog_validate

from cryolens.preprocess.stack import COGStackBuilder


def test_build_and_export_cog_validity(tmp_path: Path) -> None:
    """Verify that COGStackBuilder creates a compliant Cloud Optimized GeoTIFF."""
    builder = COGStackBuilder(output_dir=tmp_path)
    scene_id = "S1B_EW_GRDM_TEST_SCENE"

    h, w = 512, 512
    transform = from_bounds(100000.0, 500000.0, 120480.0, 520480.0, w, h)

    bands = {
        "sigma0_hh_db": np.full((h, w), -15.0, dtype=np.float32),
        "sigma0_hv_db": np.full((h, w), -28.0, dtype=np.float32),
        "ratio_hh_hv": np.full((h, w), 13.0, dtype=np.float32),
        "incidence_angle": np.full((h, w), 35.0, dtype=np.float32),
    }

    cog_file = builder.build_and_export_cog(
        scene_id=scene_id,
        bands=bands,
        transform=transform,
        crs="EPSG:3978",
    )

    assert cog_file.exists()
    assert cog_file.name == f"{scene_id}_4band_EPSG3978.tif"

    # Strict rio-cogeo validation
    is_valid, errors, warnings = cog_validate(str(cog_file))
    assert is_valid is True, f"COG validation failed: {errors}"


def test_missing_band_raises_error(tmp_path: Path) -> None:
    """Verify that missing a required band raises ValueError."""
    builder = COGStackBuilder(output_dir=tmp_path)
    incomplete_bands = {
        "sigma0_hh_db": np.zeros((10, 10), dtype=np.float32),
    }
    transform = from_bounds(0, 0, 100, 100, 10, 10)

    with pytest.raises(ValueError, match="Missing required band"):
        builder.build_and_export_cog(
            scene_id="INCOMPLETE_SCENE",
            bands=incomplete_bands,
            transform=transform,
        )
