from typing import Any

from fastapi import APIRouter, Depends, Query
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.db.repositories import IIPSightingRepository
from cryolens.db.session import get_db_session

router = APIRouter(prefix="/iip", tags=["IIP Ground Truth"])

@router.get("")
def list_iip_sightings(
    limit: int = Query(1000, le=5000),
    offset: int = 0,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve IIP iceberg sightings as GeoJSON."""
    sightings = IIPSightingRepository.list_sightings(session=session, limit=limit, offset=offset)

    features = []
    for s in sightings:
        geom_wgs84 = to_shape(s.geom_wgs84)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [geom_wgs84.x, geom_wgs84.y],
            },
            "properties": {
                "id": s.id,
                "sighting_time": s.sighting_time.isoformat() if s.sighting_time else None,
                "size_class": s.size_class,
                "shape": s.shape,
                "source": s.source,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }
