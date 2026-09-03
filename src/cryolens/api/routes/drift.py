"""FastAPI routes for iceberg drift forecasting."""

from typing import Any

from fastapi import APIRouter, Depends
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.db.repositories import DriftForecastRepository
from cryolens.db.session import get_db_session

router = APIRouter(prefix="/drift", tags=["drift"])

@router.get("/{detection_id}")
def get_drift_trajectory(detection_id: str, db: Session = Depends(get_db_session)) -> dict[str, Any]:
    """Get the forecasted drift trajectory for a specific detection as a GeoJSON FeatureCollection."""
    repo = DriftForecastRepository()
    forecasts = repo.get_trajectory(db, detection_id=detection_id)

    if not forecasts:
        return {"type": "FeatureCollection", "features": []}

    coordinates = []
    properties = {
        "detection_id": detection_id,
        "method": forecasts[0].method,
        "times": []
    }

    for f in forecasts:
        if f.geom_wgs84 is not None:
            pt = to_shape(f.geom_wgs84)
            coordinates.append([pt.x, pt.y])
            properties["times"].append(f.valid_time.isoformat())

    if not coordinates:
        return {"type": "FeatureCollection", "features": []}

    # Return as a single LineString feature
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": coordinates
        },
        "properties": properties
    }

    return {"type": "FeatureCollection", "features": [feature]}
