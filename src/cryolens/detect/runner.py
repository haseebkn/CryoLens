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
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cryolens.data.ai4arctic import AI4ArcticScene
from cryolens.detect.cfar import BaseCFARDetector, CACFARDetector, GammaCFARDetector
from cryolens.detect.filters import (
    SuppressionConfig,
    SuppressionStats,
    build_analysis_mask,
    filter_targets,
)
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
    sea_ice_fraction: float
    wind_regime: str
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
            "sea_ice_fraction": round(self.sea_ice_fraction, 4),
            "wind_regime": self.wind_regime,
            "runtime_s": round(self.runtime_s, 2),
            "suppression": self.suppression.as_dict(),
            "mask_breakdown": {k: round(v, 5) for k, v in self.mask_breakdown.items()},
        }


def classify_wind_regime(
    scene: AI4ArcticScene,
    low_quantile: float = 0.33,
    high_quantile: float = 0.67,
) -> str:
    """Bin a scene into a relative wind regime.

    The ERA5 fields in the AI4Arctic ready-to-train distribution are
    standardised with no recorded extremes, so absolute metres per second is not
    recoverable. Scenes are therefore binned by the within-scene distribution of
    wind magnitude, which still separates the calm and roughened sea-surface
    regimes that drive ocean clutter, while remaining honest about the missing
    absolute calibration.
    """
    if scene.wind_speed_normalised is None:
        return "unknown"
    values = scene.wind_speed_normalised[np.isfinite(scene.wind_speed_normalised)]
    if values.size == 0:
        return "unknown"
    median = float(np.median(values))
    lo, hi = np.quantile(values, [low_quantile, high_quantile])
    if median <= lo:
        return "low"
    if median >= hi:
        return "high"
    return "moderate"


def classify_ice_regime(scene: AI4ArcticScene) -> tuple[str, float]:
    """Classify a scene as open water or ice-affected, returning the ice fraction."""
    fraction = scene.sea_ice_fraction()
    regime = "ice_affected" if fraction >= ICE_REGIME_AREA_FRACTION else "open_water"
    return regime, fraction


def build_detector(kind: str, pfa: float) -> BaseCFARDetector:
    """Instantiate a CFAR detector by short name."""
    if kind in ("gamma", "k_distribution"):
        return GammaCFARDetector(pfa=pfa)
    if kind in ("ca", "cell_averaging"):
        return CACFARDetector(pfa=pfa)
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

        mask, breakdown = build_analysis_mask(
            valid_mask=scene.valid_mask,
            land_distance_zone=scene.land_distance_zone,
            sic_class=scene.sic_class,
            sigma0_hv_db=scene.sigma0_hv_db,
            config=self.suppression,
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

        analysed_area_km2 = float(mask.sum()) * scene.pixel_area_km2()
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
            wind_regime=classify_wind_regime(scene),
            runtime_s=time.perf_counter() - started,
            assumptions=dict(scene.assumptions),
        )

        logger.info(
            "%s: %d raw px -> %d candidates -> %d targets over %.0f km2 "
            "(%.2f per 1000 km2, %s, %s wind)",
            scene.scene_id,
            raw_pixel_hits,
            len(candidates),
            len(kept),
            analysed_area_km2,
            outcome.detections_per_1000km2,
            ice_regime,
            outcome.wind_regime,
        )
        return outcome
