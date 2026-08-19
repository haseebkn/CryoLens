"""Subswath thermal noise correction and scalloping elimination for Sentinel-1 EW GRD."""

import logging
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)


@dataclass
class SubswathBoundary:
    """Range column indices defining a subswath region and its overlap."""

    name: str  # e.g., 'EW1', 'EW2', 'EW3', 'EW4', 'EW5'
    start_col: int
    end_col: int
    overlap_with_next: tuple[int, int] | None = None  # (overlap_start, overlap_end)


class S1SubswathDenoise:
    """Implements inter-subswath thermal noise balancing (Park et al. 2018, Korosov et al. 2022).

    Sentinel-1 EW cross-polarization (HV) mode suffers from severe NESZ scalloping (-28 dB to -24 dB)
    across subswath boundaries (EW1 to EW5). Standard SNAP thermal noise subtraction often leaves
    inter-swath steps. This class balances the noise floor across adjacent subswaths using
    overlap region statistics to prevent false-alarm stripes.
    """

    def __init__(
        self,
        subswaths: list[str] | None = None,
        min_linear_floor: float = 1e-5,  # -50 dB floor to avoid log(0) or negatives
    ) -> None:
        self.subswaths = subswaths or ["EW1", "EW2", "EW3", "EW4", "EW5"]
        self.min_linear_floor = min_linear_floor

    def estimate_subswath_boundaries(
        self, width_px: int, num_subswaths: int = 5, overlap_px: int = 40
    ) -> list[SubswathBoundary]:
        """Compute synthetic or metadata-derived column boundaries for EW subswaths."""
        nominal_subswath_width = width_px // num_subswaths
        boundaries: list[SubswathBoundary] = []

        for i in range(num_subswaths):
            name = f"EW{i + 1}"
            start = max(0, i * nominal_subswath_width - (overlap_px // 2 if i > 0 else 0))
            end = min(
                width_px,
                (i + 1) * nominal_subswath_width
                + (overlap_px // 2 if i < num_subswaths - 1 else 0),
            )
            overlap = None
            if i < num_subswaths - 1:
                overlap_start = (i + 1) * nominal_subswath_width - overlap_px // 2
                overlap_end = (i + 1) * nominal_subswath_width + overlap_px // 2
                overlap = (overlap_start, overlap_end)

            boundaries.append(
                SubswathBoundary(
                    name=name,
                    start_col=start,
                    end_col=end,
                    overlap_with_next=overlap,
                )
            )

        return boundaries

    def compute_inter_subswath_factors(
        self,
        linear_intensity: NDArray[np.floating],
        noise_floor: NDArray[np.floating],
        boundaries: list[SubswathBoundary],
    ) -> dict[str, float]:
        """Estimate scaling factors k_i to balance noise floor across subswath overlaps."""
        factors: dict[str, float] = {b.name: 1.0 for b in boundaries}

        for i, b in enumerate(boundaries[:-1]):
            next_b = boundaries[i + 1]
            if not b.overlap_with_next:
                continue

            ov_s, ov_e = b.overlap_with_next
            if ov_e <= ov_s or ov_e > linear_intensity.shape[1]:
                continue

            # Extract overlap slice in linear power
            overlap_signal = linear_intensity[:, ov_s:ov_e]
            overlap_noise_left = noise_floor[:, ov_s:ov_e]

            # Robust low-percentile estimator (representing ocean clutter / noise floor)
            p10_signal = (
                np.percentile(overlap_signal[overlap_signal > 0], 10)
                if np.any(overlap_signal > 0)
                else 1e-4
            )
            mean_noise = np.mean(overlap_noise_left) if np.mean(overlap_noise_left) > 0 else 1e-4

            ratio = float(p10_signal / mean_noise)
            # Constrain factor to physically plausible range [0.7, 1.3]
            balanced_factor = float(np.clip(ratio, 0.75, 1.25))
            factors[next_b.name] = balanced_factor

        logger.debug("Computed subswath power balance factors: %s", factors)
        return factors

    def denoise(
        self,
        linear_intensity_hv: NDArray[np.floating],
        noise_equivalent_sigma0: NDArray[np.floating] | None = None,
        subswath_boundaries: list[SubswathBoundary] | None = None,
    ) -> tuple[NDArray[np.floating], dict[str, float]]:
        """Apply inter-subswath thermal noise subtraction to linear HV intensity image."""
        h, w = linear_intensity_hv.shape

        if noise_equivalent_sigma0 is None:
            # Synthetic parabolic NESZ profile across swaths (-28 dB to -24 dB range)
            x = np.linspace(-1, 1, w)
            base_nesz = 10 ** ((-28.0 + 4.0 * (x**2)) / 10.0)
            noise_equivalent_sigma0 = np.tile(base_nesz, (h, 1))

        if subswath_boundaries is None:
            subswath_boundaries = self.estimate_subswath_boundaries(w, len(self.subswaths))

        factors = self.compute_inter_subswath_factors(
            linear_intensity_hv, noise_equivalent_sigma0, subswath_boundaries
        )

        # Scale noise floor per subswath
        scaled_noise = np.copy(noise_equivalent_sigma0)
        for b in subswath_boundaries:
            factor = factors.get(b.name, 1.0)
            scaled_noise[:, b.start_col : b.end_col] *= factor

        # Subtract noise in linear power space with floor clamp
        denoised = np.maximum(
            linear_intensity_hv - scaled_noise,
            self.min_linear_floor,
        )

        return denoised.astype(np.float32), factors


def denoise_cross_pol_intensity(
    hv_intensity: NDArray[np.floating],
    nesz: NDArray[np.floating] | None = None,
) -> NDArray[np.floating]:
    """Functional interface for subswath cross-pol thermal noise removal."""
    denoiser = S1SubswathDenoise()
    denoised, _ = denoiser.denoise(hv_intensity, nesz)
    return denoised
