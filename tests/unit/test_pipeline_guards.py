"""Regression checks for geographic containment and unsupported drift."""

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import rasterio
from affine import Affine
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from cryolens.config.settings import DatabaseSettings, SpatialBBox, TilingConfig, get_app_config
from cryolens.drift.bathymetry import BathymetryManager
from cryolens.drift.forcing import ForcingManager
from cryolens.drift.model import IcebergDriftRunner
from cryolens.geo.aoi import contains_point, points_in_aoi, raster_aoi_mask
from cryolens.pipeline import PipelineRunner


def test_study_area_excludes_other_provinces_and_far_north() -> None:
    assert contains_point(-52.7, 47.56)  # St John's; land masking is separate
    assert contains_point(-50, 46)  # Grand Banks
    assert contains_point(-58, 56)  # Labrador corridor
    assert not contains_point(-63.57, 44.65)  # Halifax
    assert not contains_point(-64, 59)  # Ungava Bay
    assert not contains_point(-52, 64)  # Greenland
    assert not contains_point(float("nan"), 50)


def test_pixel_mask_removes_overlap_outside_aoi() -> None:
    transform = Affine.translation(-70, 65) * Affine.scale(1, -1)
    mask = raster_aoi_mask((30, 30), transform, "EPSG:4326")
    rows, cols = np.indices(mask.shape)
    expected = points_in_aoi(np.asarray(-70 + cols + 0.5), np.asarray(65 - rows - 0.5))
    np.testing.assert_array_equal(mask, expected)
    assert mask.any() and not mask.all()
    with pytest.raises(ValueError, match="CRS"):
        raster_aoi_mask((3, 3), transform, None)


def test_database_url_preserves_special_password_characters() -> None:
    config = DatabaseSettings(user="a@b", password="p@ss:/#%word")
    parsed = make_url(config.url)
    assert parsed.username == "a@b"
    assert parsed.password == "p@ss:/#%word"
    assert make_url(config.async_url).password == parsed.password


def test_invalid_spatial_and_tiling_configs_fail() -> None:
    with pytest.raises(ValidationError):
        SpatialBBox(west=-44, east=-60, south=42, north=60)
    with pytest.raises(ValidationError):
        TilingConfig(tile_size_px=256, tile_overlap_px=256, min_object_dim_px=2)


@pytest.mark.parametrize("manager", [ForcingManager, BathymetryManager])
def test_environmental_readers_cannot_invent_conditions(manager: type) -> None:
    model = MagicMock()
    instance = manager()
    method = getattr(instance, "attach_forcing", None) or instance.attach_bathymetry
    with pytest.raises(NotImplementedError):
        method(model)
    model.add_reader.assert_not_called()


def test_forecast_is_disabled_independently_of_optional_installation() -> None:
    with pytest.raises(NotImplementedError, match="forcing"):
        IcebergDriftRunner().run_forecast(MagicMock())


def test_pipeline_rejects_wrong_crs_before_detecting(tmp_path: Path) -> None:
    path = tmp_path / "unusable.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=4,
        dtype="float32",
        crs="EPSG:4326",
        transform=Affine.translation(-53, 50) * Affine.scale(0.01, -0.01),
    ) as dst:
        dst.write(np.full((4, 20, 20), -20, dtype=np.float32))
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.config = get_app_config()
    with pytest.raises(ValueError, match="EPSG:3978"):
        runner._run_detection(MagicMock(), path)


def test_reprocessing_preserves_existing_reviewed_results() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.session_factory = MagicMock()
    runner.cdse_client = MagicMock()
    scene = MagicMock()
    scene.footprint_geojson = {
        "type": "Polygon",
        "coordinates": [[[-53, 47], [-51, 47], [-51, 49], [-53, 49], [-53, 47]]],
    }
    from unittest.mock import patch

    with patch("cryolens.pipeline.SceneRepository.get_by_product_id") as lookup:
        lookup.return_value.status = "DETECTED"
        runner.process_scene(scene)
    runner.cdse_client.download_scene.assert_not_called()


def test_raw_pipeline_requires_explicit_unknown_ice_opt_in() -> None:
    runner = PipelineRunner.__new__(PipelineRunner)
    runner.allow_unknown_ice = False
    runner.session_factory = MagicMock()
    runner.cdse_client = MagicMock()
    scene = MagicMock()
    scene.footprint_geojson = {
        "type": "Polygon",
        "coordinates": [[[-53, 47], [-51, 47], [-51, 49], [-53, 49], [-53, 47]]],
    }
    from unittest.mock import patch

    with patch("cryolens.pipeline.SceneRepository.get_by_product_id", return_value=None):
        with pytest.raises(NotImplementedError, match="sea-ice context"):
            runner.process_scene(scene)
    runner.cdse_client.download_scene.assert_not_called()
