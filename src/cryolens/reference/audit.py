"""Bounded CFAR temporaries and a complete, globally connected candidate frame."""

from __future__ import annotations

from typing import Any

import numpy as np
from skimage.measure import label

from cryolens.data.ai4arctic import AI4ArcticScene
from cryolens.detect.cfar import BaseCFARDetector
from cryolens.detect.filters import SuppressionConfig, build_analysis_mask, filter_targets
from cryolens.detect.runner import build_detector
from cryolens.eval.cohort import digest
from cryolens.geo.aoi import points_in_aoi
from cryolens.geo.vectorize import TargetVectorizer


def tiled_detection(
    detector: BaseCFARDetector, hv: np.ndarray, hh: np.ndarray, mask: np.ndarray, core: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Halo covers the full training window; global assembly precedes connected components.

    SAR/geolocation and output arrays remain scene-sized. Only CFAR's large
    floating-point/index temporaries are bounded; this is not out-of-core ingestion.
    """
    if core < 1 or hv.shape != hh.shape or hv.shape != mask.shape:
        raise ValueError("Invalid tiled detector grid")
    hits = np.zeros(hv.shape, dtype=bool)
    eligible = np.zeros(hv.shape, dtype=bool)
    clutter = np.full(hv.shape, np.nan, dtype=np.float64)
    h, w = hv.shape
    for row in range(0, h, core):
        for col in range(0, w, core):
            end_row, end_col = min(row + core, h), min(col + core, w)
            if not mask[row:end_row, col:end_col].any():
                continue
            r0, c0 = max(0, row - detector.bg_h), max(0, col - detector.bg_w)
            r1, c1 = min(h, end_row + detector.bg_h), min(w, end_col + detector.bg_w)
            result = detector.detect(
                hv[r0:r1, c0:c1], mask[r0:r1, c0:c1], hh[r0:r1, c0:c1], max_hh_hv_ratio_db=None
            )
            inner = np.s_[row - r0 : end_row - r0, col - c0 : end_col - c0]
            hits[row:end_row, col:end_col] = result.detection_mask[inner]
            assert result.analysis_mask is not None
            eligible[row:end_row, col:end_col] = result.analysis_mask[inner]
            clutter[row:end_row, col:end_col] = result.clutter_mean_db[inner]
    return hits, eligible, clutter


def candidate_frame(
    scene: AI4ArcticScene,
    source_sha256: str,
    detector_kind: str,
    pfa: float,
    config: SuppressionConfig,
    core: int,
) -> tuple[list[dict[str, Any]], np.ndarray, dict[str, Any]]:
    mask, breakdown = build_analysis_mask(
        scene.valid_mask, scene.land_distance_zone, scene.sic_class, scene.sigma0_hv_db, config
    )
    mask &= points_in_aoi(scene.longitude, scene.latitude)
    detector = build_detector(detector_kind, pfa)
    hits, eligible, clutter = tiled_detection(
        detector, scene.sigma0_hv_db, scene.sigma0_hh_db, mask, core
    )
    targets = TargetVectorizer(min_pixels=1).extract_targets(
        hits,
        None,
        scene.sigma0_hv_db,
        scene.sigma0_hh_db,
        scene.incidence_angle_deg,
        detector.__class__.__name__,
        scene.latitude,
        scene.longitude,
        scene.pixel_spacing_m,
    )
    decisions: list[dict[str, Any]] = []
    kept, stats = filter_targets(targets, config, clutter, candidate_decisions=decisions)
    by_id = {d["target_id"]: d for d in decisions}
    # Match vectorizer membership, not all hits inside a bounding rectangle:
    # a separate component can sit inside a non-convex component's bounds.
    usable = (
        hits
        & np.isfinite(scene.sigma0_hv_db)
        & (scene.sigma0_hv_db > -90)
        & np.isfinite(scene.latitude)
        & np.isfinite(scene.longitude)
        & (np.abs(scene.latitude) <= 90)
        & (np.abs(scene.longitude) <= 180)
    )
    component_labels = label(usable, connectivity=2)
    frame = []
    for target in targets:
        r0, c0, r1, c1 = target.pixel_bbox
        coordinates = np.argwhere(component_labels[r0:r1, c0:c1] == target.target_id)
        centre = coordinates.mean(axis=0)
        nearest = coordinates[np.argmin(np.sum((coordinates - centre) ** 2, axis=1))]
        row, col = int(nearest[0]) + r0, int(nearest[1]) + c0
        frame.append(
            {
                "id": digest(
                    {
                        "source": source_sha256,
                        "bbox": list(target.pixel_bbox),
                        "target_id": target.target_id,
                    }
                ),
                "scene_id": scene.scene_id,
                "target_id": target.target_id,
                "pixel_bbox": list(target.pixel_bbox),
                "row": row,
                "col": col,
                "longitude": target.centroid_wgs84.x,
                "latitude": target.centroid_wgs84.y,
                "pixel_area": target.pixel_area,
                "peak_hv_db": target.peak_sigma0_hv_db,
                "hh_hv_ratio_db": target.hh_hv_ratio_db,
                **by_id[target.target_id],
            }
        )
    return (
        frame,
        eligible,
        {
            "raw_components": len(frame),
            "retained_components": len(kept),
            "raw_pixel_hits": int(hits.sum()),
            "eligible_pixels": int(eligible.sum()),
            "approximate_eligible_area_km2": float(eligible.sum()) * scene.pixel_area_km2(),
            "suppression": stats.as_dict(),
            "mask_breakdown": breakdown,
            "cfar_core_pixels": core,
            "cfar_halo_pixels": [detector.bg_h, detector.bg_w],
            "component_connectivity": 8,
            "components_assembled_before_suppression": True,
        },
    )
