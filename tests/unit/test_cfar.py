"""Unit tests for CFAR statistical detection engine."""

import numpy as np
import pytest

from cryolens.detect.cfar import (
    CACFARDetector,
    GammaCFARDetector,
    get_cfar_detector,
)


@pytest.fixture
def synthetic_ocean_hv() -> np.ndarray:
    """Create a 100x100 synthetic SAR HV backscatter raster with known background and targets."""
    np.random.seed(42)
    # Background ocean: exponential intensity with mean = -32 dB (approx 6.3e-4 linear)
    mean_linear = 10.0 ** (-32.0 / 10.0)
    # Exponential intensity speckle
    intensity = np.random.exponential(scale=mean_linear, size=(100, 100))
    # Convert to dB
    db_raster = 10.0 * np.log10(np.maximum(intensity, 1e-10))

    # Inject 3 distinct target spikes (icebergs with peak HV = -18 dB to -15 dB)
    # Target 1 at (30, 30)
    db_raster[29:32, 29:32] = -16.0
    # Target 2 at (70, 70)
    db_raster[69:72, 69:72] = -15.0
    # Target 3 at (50, 80)
    db_raster[50, 80] = -18.0

    return db_raster


def test_ca_cfar_detection(synthetic_ocean_hv: np.ndarray) -> None:
    """Test that CA-CFAR detects injected targets without excessive false alarms."""
    detector = CACFARDetector(
        guard_window=(2, 2),
        background_window=(10, 10),
        pfa=1e-4,
    )
    result = detector.detect(sigma0_hv_db=synthetic_ocean_hv)

    assert result.detection_mask.shape == synthetic_ocean_hv.shape
    assert result.threshold_db.shape == synthetic_ocean_hv.shape

    # Check that injected targets are detected
    assert result.detection_mask[30, 30]
    assert result.detection_mask[70, 70]
    assert result.detection_mask[50, 80]

    # Background false alarm count should be low
    total_hits = result.detection_mask.sum()
    assert 3 <= total_hits <= 25


def test_gamma_cfar_detection(synthetic_ocean_hv: np.ndarray) -> None:
    """Test that Gamma/K-CFAR detects injected targets and estimates shape parameter."""
    detector = GammaCFARDetector(
        guard_window=(2, 2),
        background_window=(12, 12),
        pfa=1e-4,
    )
    result = detector.detect(sigma0_hv_db=synthetic_ocean_hv)

    assert result.detection_mask.shape == synthetic_ocean_hv.shape
    assert result.clutter_shape is not None
    assert np.all(result.clutter_shape >= 0.1)

    # Injected targets should be detected
    assert result.detection_mask[30, 30]
    assert result.detection_mask[70, 70]


def test_cfar_guard_window_isolation() -> None:
    """Test that guard window prevents a bright target from raising its own clutter threshold."""
    arr = np.full((50, 50), -32.0)
    # Bright target in center
    arr[24:27, 24:27] = 0.0  # 0 dB (very bright target)

    detector = CACFARDetector(guard_window=(2, 2), background_window=(10, 10), pfa=1e-5)
    result = detector.detect(sigma0_hv_db=arr)

    # Center target must be detected
    assert result.detection_mask[25, 25]
    # Local clutter mean should remain close to -32 dB because target is inside guard band
    assert -33.0 <= result.clutter_mean_db[25, 25] <= -31.0


def test_dual_pol_ratio_veto() -> None:
    """Test that dual-pol ratio veto rejects high HH sea clutter without cross-pol HV contrast."""
    hv = np.full((40, 40), -32.0)
    hh = np.full((40, 40), -22.0)

    # Point 1: Iceberg with high HV (-18 dB) and HH (-15 dB) -> ratio = 3 dB (allowed)
    hv[10, 10] = -18.0
    hh[10, 10] = -15.0

    # Point 2: Sea clutter spike with low HV (-26 dB) but elevated HH (-5 dB) -> ratio = 21 dB (vetoed)
    hv[25, 25] = -24.0
    hh[25, 25] = -2.0

    detector = CACFARDetector(guard_window=(1, 1), background_window=(8, 8), pfa=1e-3)
    result = detector.detect(
        sigma0_hv_db=hv,
        sigma0_hh_db=hh,
        max_hh_hv_ratio_db=15.0,
    )

    assert result.detection_mask[10, 10]
    assert not result.detection_mask[25, 25]  # Vetoed due to excessive HH/HV ratio


def test_get_cfar_detector_factory() -> None:
    """Test factory instantiation."""
    d_ca = get_cfar_detector("cell_averaging")
    assert isinstance(d_ca, CACFARDetector)

    d_gamma = get_cfar_detector("k_distribution")
    assert isinstance(d_gamma, GammaCFARDetector)

    with pytest.raises(ValueError, match="Unknown CFAR distribution"):
        get_cfar_detector("invalid_distribution_type")  # type: ignore[arg-type]
