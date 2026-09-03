"""Unit tests for land and sea-ice mask generation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_bounds
from shapely.geometry import box

from cryolens.preprocess.masks import LandMaskGenerator, SeaIceMaskGenerator


@pytest.fixture
def synthetic_generator(tmp_path: Path) -> LandMaskGenerator:
    """A generator with no GSHHG archive, driven by an injected land polygon.

    Keeps the test independent of the 161 MB shoreline download while still
    exercising the reprojection, buffering and rasterisation path.
    """
    gen = LandMaskGenerator(
        gshhg_root=tmp_path / "missing",
        cache_path=tmp_path / "cache.gpkg",
        coastal_buffer_m=0.0,
    )
    # A land square covering the western half of the test window.
    gen.add_custom_polygon(box(-56.0, 46.0, -54.0, 50.0))
    return gen


class TestLandMask:
    """Land masking must rasterise real polygons and honour the coastal buffer."""

    def test_rasterises_injected_polygon(self, synthetic_generator: LandMaskGenerator) -> None:
        import pyproj

        h = w = 120
        t = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3978", always_xy=True)
        x0, y0 = t.transform(-58.0, 45.0)
        x1, y1 = t.transform(-52.0, 51.0)
        transform = from_bounds(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), w, h)

        mask = synthetic_generator.generate_land_mask((h, w), transform, "EPSG:3978")

        assert mask.shape == (h, w)
        assert mask.dtype == bool
        assert mask.any(), "the injected land polygon should rasterise to some pixels"
        assert not mask.all(), "the window extends beyond the land polygon"

    def test_buffer_grows_the_mask(self, synthetic_generator: LandMaskGenerator) -> None:
        import pyproj

        h = w = 120
        t = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3978", always_xy=True)
        x0, y0 = t.transform(-58.0, 45.0)
        x1, y1 = t.transform(-52.0, 51.0)
        transform = from_bounds(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), w, h)

        tight = synthetic_generator.generate_land_mask((h, w), transform, "EPSG:3978", buffer_m=0.0)
        wide = synthetic_generator.generate_land_mask(
            (h, w), transform, "EPSG:3978", buffer_m=20_000.0
        )
        assert wide.sum() > tight.sum(), "a seaward buffer must enlarge the land mask"

    def test_missing_shoreline_without_fallback_raises(self, tmp_path: Path) -> None:
        gen = LandMaskGenerator(
            gshhg_root=tmp_path / "absent", cache_path=tmp_path / "absent.gpkg"
        )
        with pytest.raises(FileNotFoundError, match="GSHHG shoreline not found"):
            gen.load_geometries()


class TestSeaIceMask:
    """Ice masking is threshold-based and defaults to open water when unknown."""

    def test_concentration_thresholding(self) -> None:
        gen = SeaIceMaskGenerator()
        field = np.array([[0.0, 0.10, 0.15, 0.90]])
        mask = gen.from_concentration(field)
        assert mask.tolist() == [[False, False, True, True]]

    def test_class_encoded_thresholding(self) -> None:
        gen = SeaIceMaskGenerator()
        sic = np.array([[0, 1, 2, 5, 10, 255]], dtype=np.uint8)
        mask = gen.from_sic_class(sic)
        # Class 2 (20 percent) is the first bin above the 15 percent ice edge;
        # 255 is the fill value and must never be read as ice.
        assert mask.tolist() == [[False, False, True, True, True, False]]

    def test_fallback_defaults_to_open_water(self) -> None:
        gen = SeaIceMaskGenerator()
        assert not gen.generate_ice_mask(shape=(20, 20), default_ice_fraction=0.05).any()
        assert gen.generate_ice_mask(shape=(20, 20), default_ice_fraction=0.85).all()

    def test_invalid_threshold_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            SeaIceMaskGenerator(concentration_threshold=1.5)

    def test_nan_concentration_treated_as_water(self) -> None:
        gen = SeaIceMaskGenerator()
        field = np.array([[np.nan, 0.9]])
        assert gen.from_concentration(field).tolist() == [[False, True]]
