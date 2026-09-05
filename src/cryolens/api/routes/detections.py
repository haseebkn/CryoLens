"""Spatially scoped detection queries and authenticated, attributable analyst reviews."""

from datetime import datetime
from typing import Any

import shapely.geometry
from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.api.query import parse_bbox, validate_dates
from cryolens.api.schemas import ValidationRequest, ValidationResponse
from cryolens.api.security import require_analyst
from cryolens.db.models import DetectionModel
from cryolens.db.repositories import DetectionRepository
from cryolens.db.session import get_db_session
from cryolens.geo.aoi import contains_point

router = APIRouter(prefix="/detections", tags=["Detections"])


def _detection_to_geojson_feature(det: DetectionModel) -> dict[str, Any]:
    geom_dict = None
    if det.geom_wgs84 is not None:
        geom_dict = shapely.geometry.mapping(to_shape(det.geom_wgs84))
    elif det.centroid_wgs84 is not None:
        geom_dict = shapely.geometry.mapping(to_shape(det.centroid_wgs84))
    latest = (
        max(det.validations, key=lambda v: (v.validated_at.isoformat(), v.id))
        if det.validations
        else None
    )
    metadata = det.properties or {}
    point = to_shape(det.centroid_wgs84) if det.centroid_wgs84 is not None else None
    expected_classes = {
        "CONFIRMED_ICEBERG": "iceberg",
        "VESSEL": "ship",
        "REJECTED_CLUTTER": "clutter",
        "OFFSHORE_STRUCTURE": "offshore_structure",
        "SEA_ICE": "sea_ice_feature",
    }
    return {
        "type": "Feature",
        "id": det.id,
        "geometry": geom_dict,
        "properties": {
            "id": det.id,
            "scene_id": det.scene_id,
            "confidence": det.confidence,
            "confidence_kind": metadata.get("confidence_kind", "uncalibrated_detector_score"),
            "assessment_status": "analyst_reviewed" if latest else "unverified_candidate",
            "display_class": expected_classes.get(latest.analyst_verdict, "unclassified")
            if latest
            else "unclassified",
            "observation_time": det.scene.acquisition_time.isoformat()
            if det.scene and det.scene.acquisition_time
            else None,
            "centroid": [point.x, point.y] if point is not None else None,
            "ais_status": metadata.get("ais_status", "unavailable"),
            "iip_association": metadata.get(
                "IIP_CORRELATED", metadata.get("iip_correlated", False)
            ),
            "detector_name": det.detector_name,
            "predicted_class": det.predicted_class,
            "length_m": det.length_m,
            "width_m": det.width_m,
            "estimated_area_m2": det.estimated_area_m2,
            "peak_sigma0_hv_db": det.peak_sigma0_hv_db,
            "mean_sigma0_hv_db": det.mean_sigma0_hv_db,
            "peak_sigma0_hh_db": det.peak_sigma0_hh_db,
            "hh_hv_ratio_db": det.hh_hv_ratio_db,
            "incidence_angle_deg": det.incidence_angle_deg,
            "created_at": det.created_at.isoformat() if det.created_at else None,
            "validated": latest is not None,
            "analyst_verdict": latest.analyst_verdict if latest else None,
            "corrected_class": latest.corrected_class if latest else None,
            "analyst_id": latest.analyst_id if latest else None,
            "validated_at": latest.validated_at.isoformat() if latest else None,
            "review_notes": latest.notes if latest else None,
            "detector_params": det.detector_params,
            "properties": metadata,
        },
    }


def _in_scope(det: DetectionModel) -> bool:
    if det.centroid_wgs84 is None or det.scene is None:
        return False
    if "DEMO" in det.scene.product_id.upper() or (det.scene.processing_provenance or {}).get(
        "synthetic"
    ):
        return False
    point = to_shape(det.centroid_wgs84)
    return contains_point(point.x, point.y)


@router.get("", response_model=dict[str, Any])
def list_detections(
    bbox: str | None = Query(
        default=None,
        description="WGS84 west,south,east,north bounds, intersected with the NL study area",
    ),
    scene_id: str | None = Query(default=None, max_length=255),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    min_confidence: float = Query(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum uncalibrated detector score; not iceberg probability",
    ),
    predicted_class: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    parsed_bbox = parse_bbox(bbox)
    validate_dates(start_date, end_date)
    detections = DetectionRepository.list_detections(
        session=session,
        bbox=parsed_bbox,
        scene_id=scene_id,
        start_date=start_date,
        end_date=end_date,
        min_confidence=min_confidence,
        predicted_class=predicted_class,
        limit=limit,
        offset=offset,
    )
    features = [_detection_to_geojson_feature(d) for d in detections if _in_scope(d)]
    return {
        "type": "FeatureCollection",
        "features": features,
        "number_returned": len(features),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if len(detections) == limit else None,
    }


@router.get("/{detection_id}", response_model=dict[str, Any])
def get_detection(detection_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    det = DetectionRepository.get_by_id(session, detection_id)
    if det is None or not _in_scope(det):
        raise HTTPException(404, "Detection not found in the NL study area.")
    return _detection_to_geojson_feature(det)


@router.post(
    "/{detection_id}/validate",
    response_model=ValidationResponse,
    status_code=status.HTTP_201_CREATED,
)
def validate_detection(
    detection_id: str,
    body: ValidationRequest,
    analyst: str = Depends(require_analyst),
    session: Session = Depends(get_db_session),
) -> ValidationResponse:
    """Keep the raw detector class unchanged and append a named analyst review."""
    det = DetectionRepository.get_by_id(session, detection_id)
    if det is None or not _in_scope(det):
        raise HTTPException(404, "Detection not found in the NL study area.")
    if body.analyst_id is not None and body.analyst_id != analyst:
        raise HTTPException(403, "analyst_id must match the configured credential identity.")
    val = DetectionRepository.record_validation(
        session=session,
        detection_id=detection_id,
        analyst_verdict=body.analyst_verdict,
        corrected_class=body.corrected_class,
        analyst_id=analyst,
        notes=body.notes,
    )
    session.commit()
    return ValidationResponse(
        id=val.id,
        detection_id=val.detection_id,
        analyst_verdict=val.analyst_verdict,
        corrected_class=val.corrected_class,
        analyst_id=val.analyst_id,
        validated_at=val.validated_at,
    )
