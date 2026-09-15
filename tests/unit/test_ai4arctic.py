"""Unit tests for the AI4Arctic reader's unit restoration and geolocation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from cryolens.data.ai4arctic import (
    AI4ArcticScene,
    SceneExtent,
    _interpolate_tiepoint_grid,
    scenes_intersecting_aoi,
)

NL_BBOX = (-64.5, 42.5, -44.0, 60.5)


def _extent(
    scene_id: str = "s",
    lat_min: float = 47.0,
    lat_max: float = 49.0,
    lon_min: float = -53.0,
    lon_max: float = -51.0,
) -> SceneExtent:
    """Build a SceneExtent for AOI filtering tests."""
    return SceneExtent(
        path=Path(f"{scene_id}.nc"),
        scene_id=scene_id,
        original_id="",
        ice_service="cis",
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
    )


class TestTiepointInterpolation:
    """The coarse geolocation grid must expand smoothly and preserve corners."""

    def test_corners_preserved(self) -> None:
        grid = np.array([[0.0, 10.0], [20.0, 30.0]])
        out = _interpolate_tiepoint_grid(grid, (5, 5))
        assert out.shape == (5, 5)
        assert out[0, 0] == pytest.approx(0.0)
        assert out[0, -1] == pytest.approx(10.0)
        assert out[-1, 0] == pytest.approx(20.0)
        assert out[-1, -1] == pytest.approx(30.0)

    def test_linear_ramp_is_reproduced(self) -> None:
        grid = np.tile(np.linspace(0.0, 4.0, 5), (5, 1))
        out = _interpolate_tiepoint_grid(grid, (9, 9))
        expected = np.tile(np.linspace(0.0, 4.0, 9), (9, 1))
        assert out == pytest.approx(expected, abs=1e-5)

    def test_monotonic_latitude_stays_monotonic(self) -> None:
        grid = np.linspace(48.0, 53.0, 21)[:, None] * np.ones((1, 21))
        out = _interpolate_tiepoint_grid(grid, (100, 80))
        column = out[:, 0]
        assert np.all(np.diff(column) >= -1e-6)


class TestAOIFiltering:
    """Scene selection must keep the working set genuinely regional."""

    def test_centre_inside_is_kept(self) -> None:
        scenes = [_extent("nl", 47.0, 49.0, -53.0, -51.0)]
        assert len(scenes_intersecting_aoi(scenes, NL_BBOX)) == 1

    def test_corner_clipper_rejected_by_centre_rule(self) -> None:
        # Ungava Bay: touches the western AOI edge, but lies mostly outside.
        ungava = _extent("ungava", 59.1, 63.4, -72.4, -63.2)
        assert ungava.intersects(NL_BBOX) is True
        assert ungava.centre_within(NL_BBOX) is False
        assert scenes_intersecting_aoi([ungava], NL_BBOX, require_centre=True) == []
        assert len(scenes_intersecting_aoi([ungava], NL_BBOX, require_centre=False)) == 1

    def test_far_scene_excluded_either_way(self) -> None:
        beaufort = _extent("beaufort", 70.0, 73.0, -140.0, -130.0)
        assert scenes_intersecting_aoi([beaufort], NL_BBOX, require_centre=False) == []

    def test_centre_computation(self) -> None:
        e = _extent(lat_min=46.0, lat_max=50.0, lon_min=-56.0, lon_max=-52.0)
        lon, lat = e.centre
        assert (lon, lat) == pytest.approx((-54.0, 48.0))

    def test_serialisation_uses_posix_paths(self) -> None:
        d = _extent("s").to_dict()
        assert "\\" not in d["path"]
        assert d["scene_id"] == "s"


class TestIceChartAvailability:
    """A withheld ice chart must never be read as zero ice concentration.

    The AI4Arctic challenge *test* scenes ship with SIC/SOD/FLOE set entirely to
    the 255 fill value; the truth lives in a separate reference file. Returning
    0.0 for those scenes would classify them as open water and silently corrupt
    every ice-stratified metric.
    """

    @staticmethod
    def _scene(sic: np.ndarray | None) -> AI4ArcticScene:
        """Build a minimal scene carrying only the fields under test."""
        z = np.zeros((4, 4), dtype=np.float32)
        return AI4ArcticScene(
            scene_id="s",
            original_id="",
            ice_service="cis",
            pixel_spacing_m=80.0,
            sigma0_hh_db=z,
            sigma0_hv_db=z,
            incidence_angle_deg=z,
            land_distance_zone=np.full((4, 4), 40, dtype=np.int16),
            latitude=z,
            longitude=z,
            sic_class=sic,
        )

    def test_all_fill_returns_none(self) -> None:
        scene = self._scene(np.full((4, 4), 255, dtype=np.uint8))
        assert scene.sea_ice_fraction() is None
        assert scene.has_ice_chart is False

    def test_absent_chart_returns_none(self) -> None:
        assert self._scene(None).sea_ice_fraction() is None

    def test_charted_open_water_returns_zero_not_none(self) -> None:
        scene = self._scene(np.zeros((4, 4), dtype=np.uint8))
        assert scene.sea_ice_fraction() == pytest.approx(0.0)
        assert scene.has_ice_chart is True

    def test_fraction_counts_only_charted_pixels(self) -> None:
        sic = np.array(
            [[0, 0, 255, 255], [5, 5, 255, 255], [0, 5, 255, 255], [255, 255, 255, 255]],
            dtype=np.uint8,
        )
        # Six charted pixels, three of them at class 5 (50 percent ice).
        assert self._scene(sic).sea_ice_fraction() == pytest.approx(0.5)


def test_publisher_standardisation_preserves_crop_values() -> None:
    from cryolens.data.ai4arctic import PUBLISHER_MEAN_STD, _restore_standardised

    mean, std = PUBLISHER_MEAN_STD["nersc_sar_secondary"]
    physical = np.array([[-40.0, -30.0, -20.0, -10.0]])
    stored = (physical - mean) / std
    recovered_crop = _restore_standardised(stored[:, 1:3], "nersc_sar_secondary")
    np.testing.assert_allclose(recovered_crop, physical[:, 1:3], atol=1e-5)


def test_publisher_distance_scaling_does_not_stretch_missing_zones() -> None:
    from cryolens.data.ai4arctic import PUBLISHER_MEAN_STD, _restore_standardised

    mean, std = PUBLISHER_MEAN_STD["distance_map"]
    zones = np.array([[5.0, 10.0, 36.0]])
    np.testing.assert_allclose(
        _restore_standardised((zones - mean) / std, "distance_map"), zones, atol=1e-5
    )


@pytest.fixture
def ready_train_scene(tmp_path: Path) -> Path:
    from netCDF4 import Dataset

    from cryolens.data.ai4arctic import PUBLISHER_MEAN_STD

    path = tmp_path / "scene_prep.nc"
    with Dataset(path, "w") as ds:
        ds.createDimension("row", 4)
        ds.createDimension("col", 4)
        ds.pixel_spacing = 80.0
        for name, physical in {
            "nersc_sar_primary": -20.0,
            "nersc_sar_secondary": -32.0,
            "distance_map": 10.0,
            "sar_incidenceangle": 35.0,
        }.items():
            variable = ds.createVariable(name, "f4", ("row", "col"))
            mean, std = PUBLISHER_MEAN_STD[name]
            variable[:] = (physical - mean) / std
            if name.startswith("nersc"):
                variable.min, variable.max = -60.0, 20.0
                variable.polarisation = "HH" if name.endswith("primary") else "HV"
        for name, physical in {"sar_grid2d_latitude": 48.0, "sar_grid2d_longitude": -53.0}.items():
            ds.createVariable(name, "f4", ("row", "col"))[:] = physical
    return path


def test_load_scene_uses_global_scaler_not_variable_extrema(ready_train_scene: Path) -> None:
    from cryolens.data.ai4arctic import load_scene

    scene = load_scene(ready_train_scene)
    np.testing.assert_allclose(scene.sigma0_hh_db, -20.0, atol=1e-4)
    np.testing.assert_allclose(scene.sigma0_hv_db, -32.0, atol=1e-4)
    assert (scene.land_distance_zone == 10).all()
    np.testing.assert_allclose(scene.incidence_angle_deg, 35.0, atol=1e-4)


def test_unknown_distance_zone_is_excluded(ready_train_scene: Path) -> None:
    from netCDF4 import Dataset

    from cryolens.data.ai4arctic import load_scene

    with Dataset(ready_train_scene, "a") as ds:
        ds["distance_map"][0, 0] = np.nan
    scene = load_scene(ready_train_scene)
    assert scene.land_mask[0, 0]


def test_mismatched_scaler_is_rejected(ready_train_scene: Path) -> None:
    from netCDF4 import Dataset

    from cryolens.data.ai4arctic import load_scene

    with Dataset(ready_train_scene, "a") as ds:
        ds["distance_map"][:] = 0.05
    with pytest.raises(ValueError, match="unsupported dataset version"):
        load_scene(ready_train_scene)
