"""False-alarm suppression for maritime SAR target detection.

Constant false alarm rate detectors are, by construction, tuned to a *pixel-wise*
probability of false alarm. On a full Sentinel-1 EW swath that still leaves a
large absolute number of false detections: a 5000x5400 raster at Pfa = 1e-5 will
produce on the order of 270 spurious pixel hits even over perfectly homogeneous
clutter, before any of the structured artefacts that dominate in practice.

This module implements the suppression stages that turn raw CFAR hits into an
operationally usable candidate list, and — importantly for evaluation — records
how many candidates each stage removed, so the false-alarm budget is auditable
rather than a single opaque number.

Stages are deliberately split into two groups:

**Pre-detection masking** decides where CFAR is allowed to look and, critically,
which pixels may contribute to clutter statistics. Land left inside the training
window biases the local mean upward and suppresses genuine nearby targets, so
this is a detection-quality issue and not merely a cosmetic filter.

**Post-detection gating** operates on vectorised candidates and rejects those
whose geometry or radiometry is inconsistent with a resolvable iceberg at
Sentinel-1 EW resolution.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cryolens.geo.vectorize import ExtractedTarget

logger = logging.getLogger(__name__)


@dataclass
class SuppressionStats:
    """Per-stage record of how many candidates a filter removed."""

    stages: list[tuple[str, int, int]] = field(default_factory=list)

    def record(self, stage: str, removed: int, remaining: int) -> None:
        """Append a stage outcome."""
        self.stages.append((stage, removed, remaining))

    @property
    def total_removed(self) -> int:
        """Total candidates removed across all stages."""
        return sum(removed for _, removed, _ in self.stages)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for storage in detection provenance."""
        return {
            "stages": [{"stage": s, "removed": r, "remaining": k} for s, r, k in self.stages],
            "total_removed": self.total_removed,
        }

    def format_table(self) -> str:
        """Render a human-readable suppression ledger."""
        if not self.stages:
            return "(no suppression stages ran)"
        width = max(len(s) for s, _, _ in self.stages)
        lines = [f"{'stage'.ljust(width)}  removed  remaining"]
        for stage, removed, remaining in self.stages:
            lines.append(f"{stage.ljust(width)}  {removed:7d}  {remaining:9d}")
        return "\n".join(lines)


@dataclass(frozen=True)
class SuppressionConfig:
    """Tunable thresholds for the suppression chain.

    Defaults target the Newfoundland and Labrador shelf at Sentinel-1 EW
    resolution (40 m pixel spacing, ~90 m resolution; 80 m for the AI4Arctic
    ready-to-train distribution). They are deliberately conservative: the
    operating point favours precision, because an analyst validation queue full
    of sea-ice clutter is worse than a slightly lower recall on targets that are
    at or below the resolution limit anyway.
    """

    # --- pre-detection masking -------------------------------------------
    coastal_buffer_zones: int = 2
    """Land-distance zones adjacent to land that are excluded. The AI4Arctic
    zonation is ordinal, not metric, so this is a zone count rather than a
    distance in kilometres."""

    border_exclusion_px: int = 64
    """Rows/columns trimmed from the swath edge, where the CFAR background
    window is truncated and noise correction is least reliable."""

    max_sic_class_for_open_water: int = 1
    """Sea ice concentration class (each step is 10 percent) above which a pixel
    is treated as ice rather than open water."""

    exclude_sea_ice: bool = False
    """When True, ice-covered pixels are removed from the analysis mask
    entirely. When False they are retained but flagged, so that detection
    performance in ice can still be measured rather than hidden."""

    seam_detection_enabled: bool = True
    seam_exclusion_px: int = 8
    seam_gradient_sigma: float = 6.0
    """Robust z-score on the range-direction gradient of the column-median HV
    profile above which a column is treated as a subswath seam."""

    max_seam_groups: int = 6
    """Ceiling on the number of contiguous seam runs retained per scene.
    Sentinel-1 EW has four interior subswath boundaries, so the physical control
    is a seam count, not a fraction of the range extent."""

    # --- post-detection gating -------------------------------------------
    min_target_pixels: int = 4
    """Minimum connected-component size. At 80 m spacing a 4-pixel cluster is
    roughly 160 m across, near the smallest reliably resolvable iceberg."""

    max_target_pixels: int = 20_000
    """Upper bound; larger connected regions are ice floes or open-water
    features, not discrete icebergs."""

    max_aspect_ratio: float = 8.0
    """Rejects long thin responses characteristic of wind streaks, ship wakes,
    and residual scalloping stripes."""

    min_contrast_db: float = 3.0
    """Minimum peak-HV excess over the locally estimated clutter floor."""

    min_peak_hv_db: float = -30.0
    """Absolute floor on peak cross-pol backscatter. Open-water HV over the
    Labrador shelf sits near -33 dB, so a target must rise meaningfully above
    that to be credible."""

    max_hh_hv_ratio_db: float = 18.0
    """Above this, the response is co-pol dominated: characteristic of specular
    sea-surface returns and metallic point targets rather than the volume
    scattering of glacial ice."""


def _robust_z(values: NDArray[np.floating]) -> NDArray[np.float64]:
    """Median-absolute-deviation z-score, resistant to the outliers being sought."""
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values, dtype=np.float64)
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    if mad <= 0.0:
        return np.zeros_like(values, dtype=np.float64)
    # 1.4826 scales MAD to a standard deviation for normal data.
    return np.asarray((values - median) / (1.4826 * mad), dtype=np.float64)


def detect_subswath_seams(
    sigma0_hv_db: NDArray[np.floating],
    valid_mask: NDArray[np.bool_] | None = None,
    sigma: float = 6.0,
    max_groups: int = 6,
) -> NDArray[np.bool_]:
    """Locate probable subswath seam columns from the range-direction profile.

    Sentinel-1 EW imagery is assembled from five subswaths whose noise floors do
    not match perfectly after thermal noise removal. The residual appears as a
    step in the column-median cross-pol profile. Rather than hardcoding subswath
    geometry — which does not survive the resampling and reprojection this
    project applies — seams are found empirically as outliers in the gradient of
    that profile.

    Args:
        max_groups: Ceiling on the number of contiguous seam runs retained.
            Sentinel-1 EW has four interior subswath boundaries; the default
            allows a little headroom without letting scene structure through.

    Returns:
        A 1-D boolean array over columns, True where a seam is suspected.
    """
    data = np.asarray(sigma0_hv_db, dtype=np.float64)
    if valid_mask is not None:
        data = np.where(valid_mask, data, np.nan)

    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        profile = np.nanmedian(data, axis=0)

    if not np.isfinite(profile).any():
        return np.zeros(data.shape[1], dtype=np.bool_)

    gradient = np.gradient(profile)
    z = np.abs(np.nan_to_num(_robust_z(gradient), nan=0.0))
    seams = np.asarray(z > sigma, dtype=np.bool_)

    n_cols = data.shape[1]
    return _keep_strongest_seam_groups(seams, z, max_groups, n_cols)


def _keep_strongest_seam_groups(
    seams: NDArray[np.bool_],
    strength: NDArray[np.float64],
    max_groups: int,
    n_cols: int,
) -> NDArray[np.bool_]:
    """Reduce flagged columns to the ``max_groups`` strongest contiguous runs.

    Capping a *fraction of columns* is the wrong control. Sentinel-1 EW is built
    from five subswaths, so there are exactly four interior boundaries; the
    physical quantity to bound is the number of seams, not how much of the range
    extent they occupy. A fractional cap on a 5000-sample swath still admits
    hundreds of columns, and once each is widened by the exclusion margin a
    scene with a sharp ice edge running across range can lose a third of its
    water to "seams" that are not seams.

    Adjacent flagged columns belong to one physical boundary, so they are first
    grouped into contiguous runs, each run scored by its peak gradient outlier,
    and only the strongest runs kept.
    """
    if not seams.any():
        return seams

    # Identify contiguous runs of flagged columns.
    padded = np.concatenate(([False], seams, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2]

    if len(starts) <= max_groups:
        logger.debug("Detected %d subswath seam group(s)", len(starts))
        return seams

    peaks = np.array([float(strength[s:e].max()) for s, e in zip(starts, ends, strict=True)])
    keep_idx = np.argsort(peaks)[-max_groups:]

    logger.info(
        "Seam test flagged %d groups across %d columns; keeping the %d strongest. "
        "Sentinel-1 EW has four interior subswath boundaries, so a larger count "
        "indicates large-scale scene structure such as an ice edge, not seams.",
        len(starts),
        n_cols,
        max_groups,
    )

    reduced = np.zeros_like(seams)
    for i in keep_idx:
        reduced[starts[i] : ends[i]] = True
    return reduced


def build_analysis_mask(
    valid_mask: NDArray[np.bool_],
    land_distance_zone: NDArray[np.integer] | None = None,
    sic_class: NDArray[np.integer] | None = None,
    sigma0_hv_db: NDArray[np.floating] | None = None,
    config: SuppressionConfig | None = None,
) -> tuple[NDArray[np.bool_], dict[str, float]]:
    """Build the mask of pixels eligible for detection and clutter estimation.

    Args:
        valid_mask: Finite-data mask for the scene.
        land_distance_zone: Ordinal land-distance zonation, 0 being land.
        sic_class: Sea ice concentration class map, 255 meaning unclassified.
        sigma0_hv_db: Cross-pol backscatter, used only for seam detection.
        config: Suppression thresholds.

    Returns:
        A tuple of the analysis mask and a dictionary of the area fraction
        removed by each masking reason, for the suppression report.
    """
    cfg = config or SuppressionConfig()
    mask = np.asarray(valid_mask, dtype=bool).copy()
    total = float(mask.size)
    breakdown: dict[str, float] = {}

    start = float(mask.sum())
    breakdown["invalid_or_nodata"] = (total - start) / total

    if land_distance_zone is not None:
        land_and_coast = land_distance_zone <= cfg.coastal_buffer_zones
        before = float(mask.sum())
        mask &= ~land_and_coast
        breakdown["land_and_coastal_buffer"] = (before - float(mask.sum())) / total

    if cfg.border_exclusion_px > 0:
        b = cfg.border_exclusion_px
        border = np.zeros_like(mask)
        border[:b, :] = True
        border[-b:, :] = True
        border[:, :b] = True
        border[:, -b:] = True
        before = float(mask.sum())
        mask &= ~border
        breakdown["swath_border"] = (before - float(mask.sum())) / total

    if cfg.seam_detection_enabled and sigma0_hv_db is not None:
        seams = detect_subswath_seams(
            sigma0_hv_db, valid_mask, cfg.seam_gradient_sigma, cfg.max_seam_groups
        )
        if seams.any():
            widened = (
                np.convolve(
                    seams.astype(np.float64),
                    np.ones(2 * cfg.seam_exclusion_px + 1),
                    mode="same",
                )
                > 0
            )
            before = float(mask.sum())
            mask &= ~widened[None, :]
            breakdown["subswath_seams"] = (before - float(mask.sum())) / total

    if cfg.exclude_sea_ice and sic_class is not None:
        ice = (sic_class > cfg.max_sic_class_for_open_water) & (sic_class != 255)
        before = float(mask.sum())
        mask &= ~ice
        breakdown["sea_ice"] = (before - float(mask.sum())) / total

    logger.info(
        "Analysis mask retains %.1f%% of the scene (%s)",
        100.0 * mask.sum() / total,
        ", ".join(f"{k} -{v * 100:.1f}%" for k, v in breakdown.items() if v > 0.0),
    )
    return mask, breakdown


def filter_targets(
    targets: list[ExtractedTarget],
    config: SuppressionConfig | None = None,
    clutter_mean_db: NDArray[np.floating] | None = None,
) -> tuple[list[ExtractedTarget], SuppressionStats]:
    """Apply post-detection gating to vectorised candidates.

    Each stage is applied in sequence and its effect recorded, producing an
    auditable ledger of where the false-alarm budget went.
    """
    cfg = config or SuppressionConfig()
    stats = SuppressionStats()
    kept = list(targets)

    def apply(stage: str, predicate: Any) -> None:
        nonlocal kept
        before = len(kept)
        kept = [t for t in kept if predicate(t)]
        stats.record(stage, before - len(kept), len(kept))

    apply("min_size", lambda t: t.pixel_area >= cfg.min_target_pixels)
    apply("max_size", lambda t: t.pixel_area <= cfg.max_target_pixels)

    def aspect_ok(t: ExtractedTarget) -> bool:
        minor = max(t.width_m, 1e-6)
        return (t.length_m / minor) <= cfg.max_aspect_ratio

    apply("aspect_ratio", aspect_ok)
    apply("min_peak_hv", lambda t: t.peak_sigma0_hv_db >= cfg.min_peak_hv_db)
    apply("copol_dominance", lambda t: t.hh_hv_ratio_db <= cfg.max_hh_hv_ratio_db)

    if clutter_mean_db is not None:

        def contrast_ok(t: ExtractedTarget) -> bool:
            r0, c0, r1, c1 = t.pixel_bbox
            r1 = max(r1, r0 + 1)
            c1 = max(c1, c0 + 1)
            local = clutter_mean_db[r0:r1, c0:c1]
            if local.size == 0 or not np.isfinite(local).any():
                return True
            return (t.peak_sigma0_hv_db - float(np.nanmedian(local))) >= cfg.min_contrast_db

        apply("clutter_contrast", contrast_ok)

    logger.info(
        "Suppression removed %d of %d candidates (%.1f%%)",
        stats.total_removed,
        len(targets),
        100.0 * stats.total_removed / max(len(targets), 1),
    )
    return kept, stats


def deduplicate_across_tiles(
    targets: list[ExtractedTarget],
    iou_threshold: float = 0.3,
    centroid_tolerance_m: float = 150.0,
) -> list[ExtractedTarget]:
    """Global non-maximum suppression across tile boundaries.

    Windowed inference emits the same physical target once per overlapping tile.
    Deduplication is done in projected coordinates rather than pixel space, so it
    is correct across tiles that do not share a pixel grid.

    Targets are ranked by confidence; a lower-ranked target is dropped when it
    either overlaps a kept target above ``iou_threshold`` or its centroid falls
    within ``centroid_tolerance_m``. The centroid rule matters because targets a
    few pixels across can straddle a seam and produce two adjacent, non-
    overlapping fragments.
    """
    if len(targets) <= 1:
        return list(targets)

    ordered = sorted(targets, key=lambda t: t.confidence, reverse=True)
    kept: list[ExtractedTarget] = []

    for candidate in ordered:
        duplicate = False
        for existing in kept:
            if (
                candidate.centroid_epsg3978.distance(existing.centroid_epsg3978)
                <= centroid_tolerance_m
            ):
                duplicate = True
                break
            inter = candidate.geom_epsg3978.intersection(existing.geom_epsg3978).area
            if inter > 0.0:
                union = candidate.geom_epsg3978.union(existing.geom_epsg3978).area
                if union > 0.0 and (inter / union) >= iou_threshold:
                    duplicate = True
                    break
        if not duplicate:
            kept.append(candidate)

    removed = len(targets) - len(kept)
    if removed:
        logger.info("Cross-tile NMS removed %d duplicate detections", removed)
    return kept
