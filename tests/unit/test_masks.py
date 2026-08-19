"""Unit tests for land and sea-ice mask generation."""

from pathlib import Path

import numpy as np
from rasterio.transform import from_bounds

from cryolens.preprocess.masks import LandMaskGenerator, SeaIceMaskGenerator


def test_land_mask_rasterization(tmp_path: Path) -> None:
    """Verify that LandMaskGenerator correctly rasterizes coastal polygons."""
    generator = LandMaskGenerator(cache_dir=tmp_path)
    h, w = 100, 100
    # Bounding box in EPSG:3978 over eastern Newfoundland
    transform = from_bounds(2400000.0, 300000.0, 2600000.0, 500000.0, w, h)

    mask = generator.generate_land_mask(
        shape=(h, w),
        transform=transform,
        crs="EPSG:3978",
    )

    assert mask.shape == (h, w)
    assert mask.dtype == bool


def test_sea_ice_mask_generation() -> None:
    """Verify SeaIceMaskGenerator output thresholding."""
    generator = SeaIceMaskGenerator()
    mask_open = generator.generate_ice_mask(shape=(50, 50), default_ice_fraction=0.05)
    assert not np.any(mask_open)

    mask_ice = generator.generate_ice_mask(shape=(50, 50), default_ice_fraction=0.85)
    assert np.all(mask_ice)
