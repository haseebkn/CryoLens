"""Unit tests for pure-Python calibration and SNAP chain execution."""

from pathlib import Path

import numpy as np
import pytest

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
    bounds = (-53.01, 47.99, -53.0, 48.0)

    result = processor.process_scene_arrays(
        hh_dn=hh_dn,
        hv_dn=hv_dn,
        source_bounds=bounds,
        source_crs="EPSG:4326",
        calibration_lut_hh=100.0,
        calibration_lut_hv=100.0,
        apply_denoise=False,
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


def test_calibrated_geocoding_uses_gcps_and_preserves_nodata() -> None:
    processor = PurePythonSARProcessor(pixel_spacing_m=100.0)
    hh = np.full((8, 8), 0.01, dtype=np.float32)
    hv = np.full((8, 8), 0.001, dtype=np.float32)
    hh[0:3, 0:3] = np.nan
    lat = np.tile(np.linspace(48.01, 48.0, 8)[:, None], (1, 8))
    lon = np.tile(np.linspace(-53.01, -53.0, 8), (8, 1))
    result = processor.process_calibrated_arrays(hh, hv, np.full((8, 8), 35.0), lat, lon)
    values = result["bands"]["sigma0_hh_db"]
    assert np.any(values == result["nodata"])
    np.testing.assert_allclose(values[values != result["nodata"]], -20.0, atol=1e-4)
    assert result["orbit_correction_applied"] is False


def test_double_denoise_rejected() -> None:
    a = np.ones((2, 2), dtype=np.float32)
    with pytest.raises(ValueError, match="already noise corrected"):
        PurePythonSARProcessor().process_calibrated_arrays(a, a, a, a, a, apply_denoise=True)


def test_invalid_calibration_lut_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        PurePythonSARProcessor().calibrate_dn_to_sigma0(np.ones((2, 2)), 0.0)


def test_snap_missing_runtime_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    safe = tmp_path / "test.SAFE"
    safe.mkdir()
    runner = SNAPChainRunner()
    monkeypatch.setattr(runner, "is_docker_available", lambda: False)
    monkeypatch.setattr(runner, "is_local_gpt_available", lambda: False)
    with pytest.raises(RuntimeError, match="requires a working"):
        runner.run_preprocessing(safe, tmp_path / "out")
    assert not list(tmp_path.rglob("*.dim"))
