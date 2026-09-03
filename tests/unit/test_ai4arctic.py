"""Unit tests for the AI4Arctic reader's unit restoration and geolocation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from cryolens.data.ai4arctic import (
    SceneExtent,
    _denormalise_linear,
    _interpolate_tiepoint_grid,
    _rescale_to_range,
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


class TestDenormalisation:
    """Physical sigma-nought must be recoverable from the standardised arrays."""

    def test_round_trip_recovers_physical_extremes(self) -> None:
        physical = np.array([[-37.9551, -20.0, 6.81137], [-10.0, 0.0, -30.0]])
        mean, std = -13.867, 6.2978
        stored = (physical - mean) / std

        recovered = _denormalise_linear(stored, -37.9551, 6.81137)
        assert recovered == pytest.approx(physical, abs=1e-3)

    def test_extremes_map_exactly(self) -> None:
        stored = np.linspace(-3.8248, 3.2834, 50).reshape(5, 10)
        out = _denormalise_linear(stored, -37.9551, 6.81137)
        assert float(np.nanmin(out)) == pytest.approx(-37.9551, abs=1e-4)
        assert float(np.nanmax(out)) == pytest.approx(6.81137, abs=1e-4)

    def test_constant_array_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="degenerate"):
            _denormalise_linear(np.ones((4, 4)), -30.0, 0.0)

    def test_all_nan_returns_nan(self) -> None:
        out = _denormalise_linear(np.full((3, 3), np.nan), -30.0, 0.0)
        assert np.isnan(out).all()

    def test_open_water_hv_is_physically_plausible(self) -> None:
        """A recovered HV field must sit well below -25 dB over open water.

        This is the physical sanity check from the project plan's Phase 1
        acceptance criteria: if the de-normalisation were wrong, the values
        would not land in the right decibel regime.
        """
        rng = np.random.default_rng(0)
        physical = rng.normal(-33.0, 2.0, size=(64, 64))
        physical[0, 0], physical[-1, -1] = -56.7, -8.0
        stored = (physical - physical.mean()) / physical.std()

        recovered = _denormalise_linear(stored, -56.7, -8.0)
        assert float(np.median(recovered)) < -25.0


class TestRescaleToRange:
    """Variables without preserved extremes are mapped onto known physical ranges."""

    def test_maps_onto_incidence_range(self) -> None:
        stored = np.linspace(-1.73, 1.52, 20).reshape(4, 5)
        out = _rescale_to_range(stored, 19.4, 47.0)
        assert float(out.min()) == pytest.approx(19.4, abs=1e-4)
        assert float(out.max()) == pytest.approx(47.0, abs=1e-4)

    def test_land_distance_zones_are_integral(self) -> None:
        zones = np.arange(42, dtype=np.float64)
        stored = (zones - zones.mean()) / zones.std()
        out = _rescale_to_range(stored, 0.0, 41.0)
        assert np.abs(out - np.rint(out)).max() < 1e-4
        assert int(np.rint(out).min()) == 0
        assert int(np.rint(out).max()) == 41

    def test_constant_input_returns_floor(self) -> None:
        out = _rescale_to_range(np.zeros((3, 3)), 5.0, 10.0)
        assert (out == 5.0).all()


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
