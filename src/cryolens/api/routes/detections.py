"""Detection query and analyst validation endpoints."""

from datetime import datetime
from typing import Any

import shapely.geometry
from fastapi import APIRouter, Depends, HTTPException, Query, status
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.api.schemas import ValidationRequest, ValidationResponse
from cryolens.db.models import DetectionModel
from cryolens.db.repositories import DetectionRepository
from cryolens.db.session import get_db_session

router = APIRouter(prefix="/detections", tags=["Detections"])


def _detection_to_geojson_feature(det: DetectionModel) -> dict[str, Any]:
    """Serialize DetectionModel to a standard GeoJSON Feature dict."""
    # Prefer polygon geometry if present, otherwise centroid point
    geom_dict = None
    if det.geom_wgs84 is not None:
        shape = to_shape(det.geom_wgs84)
        geom_dict = shapely.geometry.mapping(shape)
    elif det.centroid_wgs84 is not None:
        shape = to_shape(det.centroid_wgs84)
        geom_dict = shapely.geometry.mapping(shape)

    # Check validation status
    latest_val = det.validations[-1] if det.validations else None

    return {
        "type": "Feature",
        "id": det.id,
        "geometry": geom_dict,
        "properties": {
            "id": det.id,
            "scene_id": det.scene_id,
            "confidence": det.confidence,
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
            "validated": latest_val is not None,
            "analyst_verdict": latest_val.analyst_verdict if latest_val else None,
            "corrected_class": latest_val.corrected_class if latest_val else None,
            "detector_params": det.detector_params,
            "properties": det.properties,
        },
    }


@router.get("", response_model=dict[str, Any])
def list_detections(
    bbox: str | None = Query(
        default=None,
        description="Bounding box in format 'min_lon,min_lat,max_lon,max_lat'",
        examples=["-55.0,46.0,-48.0,50.0"],
    ),
    scene_id: str | None = Query(default=None, description="Filter by parent scene ID"),
    start_date: datetime | None = Query(
        default=None, description="Filter by scene acquisition start"
    ),
    end_date: datetime | None = Query(default=None, description="Filter by scene acquisition end"),
    min_confidence: float = Query(
        default=0.0, ge=0.0, le=1.0, description="Minimum detector confidence"
    ),
    predicted_class: str | None = Query(
        default=None, description="Filter by class (iceberg, ship, clutter)"
    ),
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Query detections with spatial and attribute filters, returning GeoJSON FeatureCollection."""
    parsed_bbox = None
    if bbox:
        try:
            parts = [float(p.strip()) for p in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("bbox must contain 4 comma-separated floats.")
            parsed_bbox = (parts[0], parts[1], parts[2], parts[3])
        except Exception as err:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid bbox parameter '{bbox}': {err}. Expected 'min_lon,min_lat,max_lon,max_lat'.",
            ) from err

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

    features = [_detection_to_geojson_feature(d) for d in detections]
    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/{detection_id}", response_model=dict[str, Any])
def get_detection(
    detection_id: str,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve single detection GeoJSON feature."""
    det = DetectionRepository.get_by_id(session, detection_id)
    if det is None:
        raise HTTPException(status_code=404, detail=f"Detection '{detection_id}' not found.")
    return _detection_to_geojson_feature(det)


@router.post(
    "/{detection_id}/validate",
    response_model=ValidationResponse,
    status_code=status.HTTP_201_CREATED,
)
def validate_detection(
    detection_id: str,
    body: ValidationRequest,
    session: Session = Depends(get_db_session),
) -> ValidationResponse:
    """Submit analyst validation verdict and corrections for a detection."""
    det = DetectionRepository.get_by_id(session, detection_id)
    if det is None:
        raise HTTPException(status_code=404, detail=f"Detection '{detection_id}' not found.")

    val = DetectionRepository.record_validation(
        session=session,
        detection_id=detection_id,
        analyst_verdict=body.analyst_verdict,
        corrected_class=body.corrected_class,
        analyst_id=body.analyst_id,
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
