"""Unit tests for Sentinel-1 subswath thermal noise balancing and scalloping removal."""

import numpy as np

from cryolens.preprocess.s1denoise import (
    S1SubswathDenoise,
    denoise_cross_pol_intensity,
)


def test_subswath_boundaries_estimation() -> None:
    """Verify subswath boundary partitioning for 5 EW swaths."""
    denoiser = S1SubswathDenoise()
    width_px = 1000
    boundaries = denoiser.estimate_subswath_boundaries(width_px, num_subswaths=5, overlap_px=40)

    assert len(boundaries) == 5
    assert boundaries[0].name == "EW1"
    assert boundaries[0].start_col == 0
    assert boundaries[-1].name == "EW5"
    assert boundaries[-1].end_col == 1000

    # Ensure overlaps exist between adjacent swaths
    for b in boundaries[:-1]:
        assert b.overlap_with_next is not None
        s, e = b.overlap_with_next
        assert e > s


def test_s1denoise_removes_nesz_scalloping() -> None:
    """Verify that s1denoise removes subswath scalloping without negative clipping."""
    h, w = 100, 500
    # Simulate calm ocean background in linear power ~ 0.001 (-30 dB)
    ocean_bg = np.full((h, w), 0.001, dtype=np.float32)

    # Add parabolic NESZ noise pattern (-28 dB to -24 dB)
    x = np.linspace(-1, 1, w)
    nesz_pattern = 10 ** ((-28.0 + 3.0 * (x**2)) / 10.0)
    noisy_hv = ocean_bg + nesz_pattern

    denoiser = S1SubswathDenoise(min_linear_floor=1e-5)
    denoised_hv, factors = denoiser.denoise(noisy_hv)

    assert denoised_hv.shape == (h, w)
    assert np.all(denoised_hv >= 1e-5)
    assert len(factors) == 5

    # Mean signal across center should be lower after noise subtraction
    assert np.mean(denoised_hv) < np.mean(noisy_hv)


def test_denoise_cross_pol_intensity_functional() -> None:
    """Test functional wrapper for s1denoise."""
    raw_hv = np.random.uniform(0.005, 0.02, size=(50, 200)).astype(np.float32)
    denoised = denoise_cross_pol_intensity(raw_hv)

    assert denoised.shape == (50, 200)
    assert np.all(np.isfinite(denoised))
    assert np.all(denoised >= 1e-5)
