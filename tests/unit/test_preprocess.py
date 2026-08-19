"""Unit tests for pure-Python calibration and SNAP chain execution."""

import numpy as np

from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.snap_chain import SNAPChainRunner


def test_calibrate_dn_to_sigma0() -> None:
    """Verify conversion of DN to linear sigma0 power."""
    processor = PurePythonSARProcessor()
    dn = np.array([[100.0, 200.0], [300.0, 400.0]], dtype=np.float32)
    lut = 100.0  # Constant calibration LUT

    # Expected: (DN / 100)^2 = [1.0, 4.0, 9.0, 16.0]
    sigma0 = processor.calibrate_dn_to_sigma0(dn, lut)
    expected = np.array([[1.0, 4.0], [9.0, 16.0]], dtype=np.float32)
    np.testing.assert_allclose(sigma0, expected, rtol=1e-5)


def test_generate_incidence_angle_grid() -> None:
    """Verify range-varying incidence angle grid generation across EW swath."""
    processor = PurePythonSARProcessor()
    grid = processor.generate_incidence_angle_grid(
        height=50, width=100, near_angle_deg=20.0, far_angle_deg=46.0
    )

    assert grid.shape == (50, 100)
    assert np.isclose(grid[0, 0], 20.0)
    assert np.isclose(grid[0, -1], 46.0)
    # Check that angles increase across columns
    assert np.all(np.diff(grid[0, :]) > 0)


def test_process_scene_arrays_reprojection() -> None:
    """Verify end-to-end array processing and reprojection to EPSG:3978."""
    processor = PurePythonSARProcessor(target_crs="EPSG:3978", pixel_spacing_m=40.0)
    h, w = 120, 150
    hh_dn = np.full((h, w), 50.0, dtype=np.float32)
    hv_dn = np.full((h, w), 10.0, dtype=np.float32)
    bounds = (-55.0, 47.0, -53.0, 49.0)

    result = processor.process_scene_arrays(
        hh_dn=hh_dn,
        hv_dn=hv_dn,
        source_bounds=bounds,
        source_crs="EPSG:4326",
        apply_denoise=True,
    )

    assert "bands" in result
    bands = result["bands"]
    assert "sigma0_hh_db" in bands
    assert "sigma0_hv_db" in bands
    assert "ratio_hh_hv" in bands
    assert "incidence_angle" in bands
    assert result["crs"] == "EPSG:3978"
    assert len(bands["sigma0_hh_db"].shape) == 2


def test_snap_chain_runner_checks() -> None:
    """Verify SNAP runner capability detection methods."""
    runner = SNAPChainRunner()
    # Ensure helper methods return boolean without unhandled exceptions
    assert isinstance(runner.is_docker_available(), bool)
    assert isinstance(runner.is_local_gpt_available(), bool)
