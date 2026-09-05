"""Scene-level detection runner over real Sentinel-1 EW data.

Ties the pieces into one auditable pass: build the analysis mask, run CFAR in
linear power space, vectorise connected components, apply false-alarm
suppression, and report the detection density per 1000 square kilometres of
water actually examined.

The area denominator matters. Reporting false alarms per scene is meaningless
when swath coverage, land fraction, and masking differ between acquisitions, so
every rate here is normalised by the analysed water area rather than by scene
count.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from cryolens.data.ai4arctic import AI4ArcticScene
from cryolens.detect.cfar import BaseCFARDetector, get_cfar_detector
from cryolens.detect.filters import (
    SuppressionConfig,
    SuppressionStats,
    build_analysis_mask,
    filter_targets,
)
from cryolens.geo.aoi import points_in_aoi
from cryolens.geo.vectorize import ExtractedTarget, TargetVectorizer

logger = logging.getLogger(__name__)

# Sea ice concentration class at or above which a scene is treated as
# ice-affected for stratified reporting. Class 2 corresponds to 20 percent,
# the first bin above the conventional 15 percent ice-edge definition.
ICE_REGIME_SIC_CLASS = 2
ICE_REGIME_AREA_FRACTION = 0.15


@dataclass
class SceneDetectionResult:
    """Outcome of one scene pass, including the suppression ledger."""

    scene_id: str
    detector_name: str
    pfa: float
    targets: list[ExtractedTarget]
    raw_pixel_hits: int
    raw_candidates: int
    analysed_area_km2: float
    suppression: SuppressionStats
    mask_breakdown: dict[str, float]
    ice_regime: str
    sea_ice_fraction: float | None
    wind_regime: str
    """Assigned by :func:`assign_wind_regimes` once the whole cohort is known."""

    wind_statistic: float | None
    """Scene median physical wind speed in m/s; None when absent."""

    runtime_s: float
    assumptions: dict[str, str] = field(default_factory=dict)

    @property
    def detections_per_1000km2(self) -> float:
        """Final detection density over the analysed water area."""
        if self.analysed_area_km2 <= 0.0:
            return 0.0
        return 1000.0 * len(self.targets) / self.analysed_area_km2

    @property
    def raw_candidates_per_1000km2(self) -> float:
        """Pre-suppression candidate density, for measuring suppression gain."""
        if self.analysed_area_km2 <= 0.0:
            return 0.0
        return 1000.0 * self.raw_candidates / self.analysed_area_km2

    @property
    def suppression_factor(self) -> float:
        """Ratio of candidates before to after suppression."""
        return self.raw_candidates / max(len(self.targets), 1)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for aggregation and reporting."""
        return {
            "scene_id": self.scene_id,
            "detector": self.detector_name,
            "pfa": self.pfa,
            "n_targets": len(self.targets),
            "raw_pixel_hits": self.raw_pixel_hits,
            "raw_candidates": self.raw_candidates,
            "analysed_area_km2": round(self.analysed_area_km2, 1),
            "detections_per_1000km2": round(self.detections_per_1000km2, 3),
            "raw_candidates_per_1000km2": round(self.raw_candidates_per_1000km2, 3),
            "suppression_factor": round(self.suppression_factor, 2),
            "ice_regime": self.ice_regime,
            "sea_ice_fraction": (
                None if self.sea_ice_fraction is None else round(self.sea_ice_fraction, 4)
            ),
            "wind_regime": self.wind_regime,
            "wind_statistic": (
                None if self.wind_statistic is None else round(self.wind_statistic, 5)
            ),
            "runtime_s": round(self.runtime_s, 2),
            "suppression": self.suppression.as_dict(),
            "mask_breakdown": {k: round(v, 5) for k, v in self.mask_breakdown.items()},
            "assumptions": self.assumptions,
        }


def scene_wind_statistic(scene: AI4ArcticScene) -> float | None:
    """Summarise a scene's wind field as a single scalar, or None if absent.

    Wind components are restored with the publisher's verified per-variable
    mean and standard deviation before magnitude is computed. Values are m/s;
    subsequent low/moderate/high categories remain relative cohort terciles,
    not Beaufort classes or operational weather thresholds.
    """
    if scene.wind_speed_m_s is None:
        return None
    values = scene.wind_speed_m_s[np.isfinite(scene.wind_speed_m_s)]
    if values.size == 0:
        return None
    return float(np.median(values))


def assign_wind_regimes(
    results: list[SceneDetectionResult],
    low_quantile: float = 1.0 / 3.0,
    high_quantile: float = 2.0 / 3.0,
) -> None:
    """Assign low/moderate/high wind regimes by terciles **across the cohort**.

    Binning must be done between scenes, not within one. A scene's own median
    lies between its own 33rd and 67th percentiles by construction, so a
    within-scene comparison labels every scene "moderate" and produces a
    stratification table that says nothing. Terciles are therefore computed over
    the per-scene statistics of the whole run.

    Scenes with no wind field keep the "unknown" regime and are excluded from
    the tercile computation so they cannot shift the boundaries.
    """
    if not 0 <= low_quantile < high_quantile <= 1:
        raise ValueError("Wind quantiles must be ordered between 0 and 1")
    for r in results:
        r.wind_regime = "unknown"
    measured = [
        r for r in results if r.wind_statistic is not None and np.isfinite(r.wind_statistic)
    ]
    if len(measured) < 3:
        for r in measured:
            r.wind_regime = "unknown"
        logger.warning(
            "Only %d scenes carry wind data; too few to form terciles, so wind "
            "stratification is reported as unknown.",
            len(measured),
        )
        return

    stats = np.array([r.wind_statistic for r in measured], dtype=np.float64)
    lo, hi = np.quantile(stats, [low_quantile, high_quantile])
    if lo == hi:
        return

    for r in measured:
        assert r.wind_statistic is not None
        if r.wind_statistic <= lo:
            r.wind_regime = "low"
        elif r.wind_statistic >= hi:
            r.wind_regime = "high"
        else:
            r.wind_regime = "moderate"

    logger.info(
        "Wind terciles across %d scenes: low <= %.4f < moderate < %.4f <= high "
        "(m/s; relative cohort bins)",
        len(measured),
        float(lo),
        float(hi),
    )


def classify_ice_regime(scene: AI4ArcticScene) -> tuple[str, float | None]:
    """Classify a scene as open water, ice-affected, or unknown.

    Scenes without a usable ice chart return ``"unknown"`` rather than being
    folded into open water. The AI4Arctic challenge test scenes have their ice
    labels withheld, and treating an absent chart as zero ice concentration
    would put them in the wrong stratum and understate detection density over
    ice.
    """
    fraction = scene.sea_ice_fraction()
    if fraction is None:
        return "unknown", None
    regime = "ice_affected" if fraction >= ICE_REGIME_AREA_FRACTION else "open_water"
    return regime, fraction


def build_detector(kind: str, pfa: float) -> BaseCFARDetector:
    """Instantiate a CFAR detector by short name."""
    if kind in ("gamma", "k_distribution"):
        return get_cfar_detector(distribution="gamma", pfa=pfa)
    if kind in ("ca", "cell_averaging"):
        return get_cfar_detector(distribution="cell_averaging", pfa=pfa)
    raise ValueError(f"Unknown detector kind: {kind!r}")


class SceneDetectionRunner:
    """Runs the full detect-and-suppress chain over one scene."""

    def __init__(
        self,
        detector_kind: str = "gamma",
        pfa: float = 1e-5,
        suppression: SuppressionConfig | None = None,
    ) -> None:
        """Configure the detector and suppression thresholds."""
        self.detector_kind = detector_kind
        self.pfa = pfa
        self.suppression = suppression or SuppressionConfig()
        self.vectorizer = TargetVectorizer(min_pixels=1)

    def run(self, scene: AI4ArcticScene) -> SceneDetectionResult:
        """Detect targets in ``scene`` and return the result with its audit trail."""
        started = time.perf_counter()
        provenance = dict(scene.assumptions)
        provenance["source_product_id"] = scene.original_id
        timestamp = re.search(r"S1[ABC]_EW_GRDM_1SDH_(\d{8}T\d{6})_", scene.original_id)
        if timestamp:
            provenance["source_acquisition_time"] = (
                datetime.strptime(timestamp.group(1), "%Y%m%dT%H%M%S")
                .replace(tzinfo=UTC)
                .isoformat()
            )

        mask, breakdown = build_analysis_mask(
            valid_mask=scene.valid_mask,
            land_distance_zone=scene.land_distance_zone,
            sic_class=scene.sic_class,
            sigma0_hv_db=scene.sigma0_hv_db,
            config=self.suppression,
        )
        before_aoi = int(mask.sum())
        mask &= points_in_aoi(scene.longitude, scene.latitude)
        breakdown["outside_nl_study_area"] = (before_aoi - int(mask.sum())) / mask.size
        if not mask.any():
            ice_regime, ice_fraction = classify_ice_regime(scene)
            return SceneDetectionResult(
                scene.scene_id,
                self.detector_kind,
                self.pfa,
                [],
                0,
                0,
                0.0,
                SuppressionStats(),
                breakdown,
                ice_regime,
                ice_fraction,
                "unknown",
                scene_wind_statistic(scene),
                time.perf_counter() - started,
                provenance,
            )

        detector = build_detector(self.detector_kind, self.pfa)
        result = detector.detect(
            sigma0_hv_db=scene.sigma0_hv_db,
            valid_mask=mask,
            sigma0_hh_db=scene.sigma0_hh_db,
            max_hh_hv_ratio_db=None,  # applied as an auditable post-detection stage
        )

        raw_pixel_hits = int(result.detection_mask.sum())

        candidates = self.vectorizer.extract_targets(
            detection_mask=result.detection_mask,
            transform=None,
            sigma0_hv_db=scene.sigma0_hv_db,
            sigma0_hh_db=scene.sigma0_hh_db,
            incidence_angle=scene.incidence_angle_deg,
            detector_name=detector.__class__.__name__,
            latitude=scene.latitude,
            longitude=scene.longitude,
            pixel_spacing_m=scene.pixel_spacing_m,
        )

        kept, stats = filter_targets(
            candidates,
            config=self.suppression,
            clutter_mean_db=result.clutter_mean_db,
        )

        analysis_mask = result.analysis_mask if result.analysis_mask is not None else mask
        breakdown["insufficient_training_support"] = (
            int(mask.sum()) - int(analysis_mask.sum())
        ) / mask.size
        analysed_area_km2 = float(analysis_mask.sum()) * scene.pixel_area_km2()
        ice_regime, ice_fraction = classify_ice_regime(scene)

        outcome = SceneDetectionResult(
            scene_id=scene.scene_id,
            detector_name=detector.__class__.__name__,
            pfa=self.pfa,
            targets=kept,
            raw_pixel_hits=raw_pixel_hits,
            raw_candidates=len(candidates),
            analysed_area_km2=analysed_area_km2,
            suppression=stats,
            mask_breakdown=breakdown,
            ice_regime=ice_regime,
            sea_ice_fraction=ice_fraction,
            wind_regime="unknown",  # resolved by assign_wind_regimes over the cohort
            wind_statistic=scene_wind_statistic(scene),
            runtime_s=time.perf_counter() - started,
            assumptions=provenance,
        )

        logger.info(
            "%s: %d raw px -> %d candidates -> %d targets over %.0f km2 (%.2f per 1000 km2, %s)",
            scene.scene_id,
            raw_pixel_hits,
            len(candidates),
            len(kept),
            analysed_area_km2,
            outcome.detections_per_1000km2,
            ice_regime,
        )
        return outcome
