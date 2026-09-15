"""Time-filtered IIP observations provide context, not automatic target confirmation."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.api.query import parse_bbox, validate_dates
from cryolens.db.repositories import IIPSightingRepository
from cryolens.db.session import get_db_session

router = APIRouter(prefix="/iip", tags=["IIP observations"])


@router.get("")
def list_iip_sightings(
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    bbox: str | None = Query(None),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    validate_dates(start_date, end_date)
    sightings = IIPSightingRepository.list_sightings(
        session=session,
        limit=limit,
        offset=offset,
        bbox=parse_bbox(bbox),
        start_date=start_date,
        end_date=end_date,
    )
    features = []
    for sighting in sightings:
        point = to_shape(sighting.geom_wgs84)
        features.append(
            {
                "type": "Feature",
                "id": sighting.id,
                "geometry": {"type": "Point", "coordinates": [point.x, point.y]},
                "properties": {
                    "id": sighting.id,
                    "sighting_time": sighting.sighting_time.isoformat()
                    if sighting.sighting_time
                    else None,
                    "size_class": sighting.size_class,
                    "shape": sighting.shape,
                    "source": sighting.source,
                    "interpretation": "Historical observation; proximity alone does not confirm a SAR target",
                },
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
        "number_returned": len(features),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if len(sightings) == limit else None,
    }
