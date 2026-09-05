"""Statistical Constant False Alarm Rate (CFAR) detection engine in linear power space."""

import warnings
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray
from scipy.stats import gamma

from cryolens.config.settings import get_project_config


@dataclass(frozen=True)
class CFARResult:
    """Detection results and intermediate diagnostic maps from CFAR execution."""

    detection_mask: np.ndarray  # 2D boolean mask of positive hits
    snr_ratio: np.ndarray  # Linear target-to-threshold ratio (I_CUT / T)
    threshold_db: np.ndarray  # Computed detection threshold in dB
    clutter_mean_db: np.ndarray  # Estimated local clutter floor in dB
    clutter_shape: np.ndarray | None  # Estimated Gamma shape parameter nu (if applicable)
    analysis_mask: np.ndarray | None = None  # CUTs with sufficient valid training support


def _compute_integral_image(arr: np.ndarray) -> NDArray[np.float64]:
    """Compute 2D integral image (prefix sum) with zero-padded top and left borders."""
    h, w = arr.shape
    integral: NDArray[np.float64] = np.zeros((h + 1, w + 1), dtype=np.float64)
    # 2D cumulative sum
    integral[1:, 1:] = np.cumsum(np.cumsum(arr, axis=0), axis=1)
    return integral


def _query_box_sum(
    integral: NDArray[np.float64],
    r_min: np.ndarray,
    r_max: np.ndarray,
    c_min: np.ndarray,
    c_max: np.ndarray,
) -> NDArray[np.float64]:
    """Query 2D sum of rectangle [r_min, r_max] x [c_min, c_max] in O(1) time."""
    # Note: r_max, c_max are inclusive indices in [0, H-1], [0, W-1]
    result = (
        integral[r_max + 1, c_max + 1]
        - integral[r_min, c_max + 1]
        - integral[r_max + 1, c_min]
        + integral[r_min, c_min]
    )
    return cast(NDArray[np.float64], result)


class BaseCFARDetector:
    """Base class for 2D sliding-window CFAR detectors."""

    def __init__(
        self,
        guard_window: tuple[int, int] = (3, 3),
        background_window: tuple[int, int] = (15, 15),
        pfa: float = 1e-5,
        min_linear_intensity: float = 1e-6,
        min_training_fraction: float = 0.5,
    ) -> None:
        """Initialize CFAR detector with window half-widths and target Pfa."""
        self.guard_h, self.guard_w = guard_window
        self.bg_h, self.bg_w = background_window
        self.pfa = pfa
        self.min_linear_intensity = min_linear_intensity
        self.min_training_fraction = min_training_fraction

        if any(
            not isinstance(v, (int, np.integer)) or v < 0
            for v in (*guard_window, *background_window)
        ):
            raise ValueError("Window half-widths must be nonnegative integers")
        if not np.isfinite(pfa) or not 0.0 < pfa < 1.0:
            raise ValueError("pfa must be finite and strictly between 0 and 1")
        if not np.isfinite(min_linear_intensity) or min_linear_intensity <= 0:
            raise ValueError("min_linear_intensity must be finite and positive")
        if not 0.0 < min_training_fraction <= 1.0:
            raise ValueError("min_training_fraction must be in (0, 1]")

        if self.guard_h >= self.bg_h or self.guard_w >= self.bg_w:
            raise ValueError(
                f"Guard window {guard_window} must be strictly smaller than background window {background_window}."
            )

    def _prepare_input(
        self, hv: np.ndarray, valid_mask: np.ndarray | None, hh: np.ndarray | None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Combine caller masks with actual data validity before any prefix sum."""
        hv = np.asarray(hv, dtype=np.float64)
        if hv.ndim != 2 or hv.size == 0:
            raise ValueError("Backscatter must be a nonempty 2D array")
        if valid_mask is not None and np.shape(valid_mask) != hv.shape:
            raise ValueError("valid_mask must match the backscatter shape")
        if hh is not None and np.shape(hh) != hv.shape:
            raise ValueError("HH must match the HV backscatter shape")
        valid = np.isfinite(hv) & (hv > -90.0)
        if valid_mask is not None:
            valid &= np.asarray(valid_mask, dtype=bool)
        with np.errstate(over="ignore", invalid="ignore"):
            power = np.power(10.0, np.where(valid, hv, -90.0) / 10.0)
            valid &= np.isfinite(power) & (power < np.sqrt(np.finfo(float).max))
        return np.where(valid, np.maximum(power, self.min_linear_intensity), 0.0), valid

    def _analysis_mask(self, valid_mask: np.ndarray, count: np.ndarray, minimum: int) -> np.ndarray:
        nominal = (2 * self.bg_h + 1) * (2 * self.bg_w + 1) - (2 * self.guard_h + 1) * (
            2 * self.guard_w + 1
        )
        return valid_mask & (
            count >= max(minimum, int(np.ceil(nominal * self.min_training_fraction)))
        )

    def _extract_training_statistics(
        self,
        linear_intensity: np.ndarray,
        valid_mask: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Compute training sum, training sum of squares, and valid count via integral images."""
        h, w = linear_intensity.shape

        # Clean invalid values for sum accumulation
        safe_intensity = np.where(valid_mask, linear_intensity, 0.0)
        safe_sq = np.where(valid_mask, linear_intensity**2, 0.0)
        mask_f64 = valid_mask.astype(np.float64)

        int_sum = _compute_integral_image(safe_intensity)
        int_sq = _compute_integral_image(safe_sq)
        int_cnt = _compute_integral_image(mask_f64)

        # Generate grid indices
        r_indices, c_indices = np.indices((h, w))

        # Outer background bounding box (clipped to array bounds)
        bg_r_min = np.clip(r_indices - self.bg_h, 0, h - 1)
        bg_r_max = np.clip(r_indices + self.bg_h, 0, h - 1)
        bg_c_min = np.clip(c_indices - self.bg_w, 0, w - 1)
        bg_c_max = np.clip(c_indices + self.bg_w, 0, w - 1)

        # Inner guard bounding box (clipped to array bounds)
        gd_r_min = np.clip(r_indices - self.guard_h, 0, h - 1)
        gd_r_max = np.clip(r_indices + self.guard_h, 0, h - 1)
        gd_c_min = np.clip(c_indices - self.guard_w, 0, w - 1)
        gd_c_max = np.clip(c_indices + self.guard_w, 0, w - 1)

        # Background sums minus guard sums
        bg_sum = _query_box_sum(int_sum, bg_r_min, bg_r_max, bg_c_min, bg_c_max)
        gd_sum = _query_box_sum(int_sum, gd_r_min, gd_r_max, gd_c_min, gd_c_max)
        train_sum = bg_sum - gd_sum

        bg_sq = _query_box_sum(int_sq, bg_r_min, bg_r_max, bg_c_min, bg_c_max)
        gd_sq = _query_box_sum(int_sq, gd_r_min, gd_r_max, gd_c_min, gd_c_max)
        train_sq = bg_sq - gd_sq

        bg_cnt = _query_box_sum(int_cnt, bg_r_min, bg_r_max, bg_c_min, bg_c_max)
        gd_cnt = _query_box_sum(int_cnt, gd_r_min, gd_r_max, gd_c_min, gd_c_max)
        train_cnt = bg_cnt - gd_cnt

        return train_sum, train_sq, train_cnt

    def detect(
        self,
        sigma0_hv_db: np.ndarray,
        valid_mask: np.ndarray | None = None,
        sigma0_hh_db: np.ndarray | None = None,
        max_hh_hv_ratio_db: float | None = 20.0,
    ) -> CFARResult:
        """Run CFAR detection on SAR backscatter."""
        raise NotImplementedError


class CACFARDetector(BaseCFARDetector):
    """Cell-Averaging CFAR (CA-CFAR) on linear power intensity."""

    def detect(
        self,
        sigma0_hv_db: np.ndarray,
        valid_mask: np.ndarray | None = None,
        sigma0_hh_db: np.ndarray | None = None,
        max_hh_hv_ratio_db: float | None = 20.0,
    ) -> CFARResult:
        """Execute CA-CFAR on linear intensity."""
        linear_hv, valid_mask = self._prepare_input(sigma0_hv_db, valid_mask, sigma0_hh_db)

        train_sum, _, train_cnt = self._extract_training_statistics(linear_hv, valid_mask)

        # Minimum required training cells
        min_cells = 8
        safe_cnt = np.maximum(train_cnt, min_cells)

        # Local clutter mean power
        mu_clutter = train_sum / safe_cnt

        # CA-CFAR scaling factor: alpha = N * (P_fa^(-1/N) - 1)
        alpha = safe_cnt * np.expm1(-np.log(self.pfa) / safe_cnt)
        threshold_linear = alpha * mu_clutter

        # Cell Under Test (CUT) detection condition
        analysis_mask = self._analysis_mask(valid_mask, train_cnt, min_cells)
        hits = (linear_hv > threshold_linear) & analysis_mask

        # Optional Dual-pol ratio cross-check veto:
        # Reject clutter spikes where HH is excessively larger than HV (no volume scattering)
        if sigma0_hh_db is not None and max_hh_hv_ratio_db is not None:
            ratio_db = sigma0_hh_db - sigma0_hv_db
            hits &= (
                np.isfinite(sigma0_hh_db)
                & (sigma0_hh_db > -90.0)
                & (ratio_db <= max_hh_hv_ratio_db)
            )

        # Diagnostic maps
        threshold_db = 10.0 * np.log10(np.maximum(threshold_linear, 1e-12))
        clutter_mean_db = 10.0 * np.log10(np.maximum(mu_clutter, 1e-12))
        snr_ratio = np.divide(
            linear_hv,
            threshold_linear,
            out=np.zeros_like(linear_hv, dtype=np.float64),
            where=threshold_linear > 0,
        )

        return CFARResult(
            detection_mask=hits,
            snr_ratio=snr_ratio,
            threshold_db=threshold_db,
            clutter_mean_db=clutter_mean_db,
            clutter_shape=None,
            analysis_mask=analysis_mask,
        )


class GammaCFARDetector(BaseCFARDetector):
    """Adaptive Gamma threshold with method-of-moments shape estimation.

    This is not a compound K-distribution detector. The plug-in quantile does
    not guarantee the design Pfa on finite, correlated or non-Gamma clutter.
    """

    _NU_MIN = 0.1
    _NU_MAX = 50.0
    _NU_GRID_POINTS = 1024

    def _unit_gamma_quantile(self, nu: np.ndarray) -> NDArray[np.float64]:
        """Evaluate ppf(1 - Pfa; shape=nu, scale=1) via interpolation over log-nu.

        Interpolating in log space keeps the grid dense where the quantile
        changes fastest (small shape parameters, i.e. the heavy-tailed high sea
        state regime that actually drives false alarms).
        """
        log_grid = np.linspace(np.log(self._NU_MIN), np.log(self._NU_MAX), self._NU_GRID_POINTS)
        nu_grid = np.exp(log_grid)
        q_grid = gamma.isf(self.pfa, a=nu_grid, scale=1.0)

        log_nu = np.log(np.clip(nu, self._NU_MIN, self._NU_MAX))
        return np.asarray(np.interp(log_nu, log_grid, q_grid), dtype=np.float64)

    def detect(
        self,
        sigma0_hv_db: np.ndarray,
        valid_mask: np.ndarray | None = None,
        sigma0_hh_db: np.ndarray | None = None,
        max_hh_hv_ratio_db: float | None = 20.0,
    ) -> CFARResult:
        """Execute adaptive Gamma thresholding using local mean and variance."""
        linear_hv, valid_mask = self._prepare_input(sigma0_hv_db, valid_mask, sigma0_hh_db)

        train_sum, train_sq, train_cnt = self._extract_training_statistics(linear_hv, valid_mask)

        min_cells = 12
        safe_cnt = np.maximum(train_cnt, min_cells)

        mu_hat = train_sum / safe_cnt
        m2_hat = train_sq / safe_cnt
        # Unbiased sample variance
        var_hat = np.maximum(
            (safe_cnt / (safe_cnt - 1.0)) * (m2_hat - mu_hat**2), np.finfo(float).tiny
        )

        # Gamma mean = shape * scale; variance = shape * scale**2.
        # Subtracting mu**2/N here incorrectly narrows the fitted clutter tail.
        with np.errstate(over="ignore"):
            nu_hat = np.clip(mu_hat**2 / var_hat, self._NU_MIN, self._NU_MAX)

        # Quantile calculation: Gamma(shape=nu, scale=mu/nu).
        # gamma.ppf is a root-find per element and is intractable evaluated
        # pixel-wise on a full swath (order 10^7 pixels). Because the Gamma
        # quantile is exactly linear in the scale parameter,
        #     ppf(q; nu, scale) == scale * ppf(q; nu, 1),
        # the shape-dependent part is a function of nu alone and is evaluated
        # once on a log-spaced grid, then interpolated. This is exact up to the
        # interpolation error in nu, which is bounded below by the clipping in
        # _NU_MIN/_NU_MAX and is far smaller than the estimation error in nu.
        scale_hat = mu_hat / nu_hat
        unit_quantile = self._unit_gamma_quantile(nu_hat)
        threshold_linear = scale_hat * unit_quantile

        analysis_mask = self._analysis_mask(valid_mask, train_cnt, min_cells)
        hits = (linear_hv > threshold_linear) & analysis_mask

        if sigma0_hh_db is not None and max_hh_hv_ratio_db is not None:
            ratio_db = sigma0_hh_db - sigma0_hv_db
            hits &= (
                np.isfinite(sigma0_hh_db)
                & (sigma0_hh_db > -90.0)
                & (ratio_db <= max_hh_hv_ratio_db)
            )

        threshold_db = 10.0 * np.log10(np.maximum(threshold_linear, 1e-12))
        clutter_mean_db = 10.0 * np.log10(np.maximum(mu_hat, 1e-12))
        snr_ratio = np.divide(
            linear_hv,
            threshold_linear,
            out=np.zeros_like(linear_hv, dtype=np.float64),
            where=threshold_linear > 0,
        )

        return CFARResult(
            detection_mask=hits,
            snr_ratio=snr_ratio,
            threshold_db=threshold_db,
            clutter_mean_db=clutter_mean_db,
            clutter_shape=nu_hat,
            analysis_mask=analysis_mask,
        )


def get_cfar_detector(
    distribution: Literal["cell_averaging", "k_distribution", "gamma"] | None = None,
    guard_window: tuple[int, int] | None = None,
    background_window: tuple[int, int] | None = None,
    pfa: float | None = None,
) -> BaseCFARDetector:
    """Factory function to instantiate CFAR detector from parameters or project config."""
    cfg = get_project_config().cfar

    dist_name = distribution or cfg.distribution
    gw = guard_window or (cfg.guard_window[0], cfg.guard_window[1])
    bw = background_window or (cfg.background_window[0], cfg.background_window[1])
    target_pfa = pfa if pfa is not None else cfg.default_pfa

    if dist_name in ("k_distribution", "gamma"):
        if dist_name == "k_distribution":
            warnings.warn(
                "k_distribution is a deprecated alias for Gamma; no K-distribution model is implemented",
                DeprecationWarning,
                stacklevel=2,
            )
        return GammaCFARDetector(guard_window=gw, background_window=bw, pfa=target_pfa)
    elif dist_name == "cell_averaging":
        return CACFARDetector(guard_window=gw, background_window=bw, pfa=target_pfa)
    else:
        raise ValueError(f"Unknown CFAR distribution type: '{dist_name}'")
