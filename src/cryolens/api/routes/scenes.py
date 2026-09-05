"""Scene query endpoints for the configured NL shelf study area."""

from datetime import datetime
from typing import Any

import shapely.geometry
from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.api.query import validate_dates
from cryolens.db.models import SceneModel
from cryolens.db.repositories import SceneRepository
from cryolens.db.session import get_db_session
from cryolens.geo.aoi import contains_point, load_aoi, scene_intersects_aoi

router = APIRouter(prefix="/scenes", tags=["Scenes"])


def _scene_to_geojson_feature(scene: SceneModel) -> dict[str, Any]:
    geom = (
        shapely.geometry.mapping(to_shape(scene.footprint_wgs84).intersection(load_aoi()))
        if scene.footprint_wgs84 is not None
        else None
    )
    count = sum(
        1
        for d in scene.detections
        if d.centroid_wgs84 is not None
        and contains_point(to_shape(d.centroid_wgs84).x, to_shape(d.centroid_wgs84).y)
    )
    return {
        "type": "Feature",
        "id": scene.id,
        "geometry": geom,
        "properties": {
            "id": scene.id,
            "product_id": scene.product_id,
            "platform": scene.platform,
            "mode": scene.mode,
            "polarizations": scene.polarizations,
            "acquisition_time": scene.acquisition_time.isoformat()
            if scene.acquisition_time
            else None,
            "status": scene.status,
            "raster_available": bool(scene.cog_path),
            "detection_count": count,
            "processing_provenance": scene.processing_provenance,
            "created_at": scene.created_at.isoformat() if scene.created_at else None,
        },
    }


def _publishable(scene: SceneModel) -> bool:
    return "DEMO" not in scene.product_id.upper() and not (scene.processing_provenance or {}).get(
        "synthetic"
    )


@router.get("", response_model=dict[str, Any])
def list_scenes(
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    validate_dates(start_date, end_date)
    records = SceneRepository.list_scenes(
        session=session,
        start_date=start_date,
        end_date=end_date,
        status=status,
        limit=limit,
        offset=offset,
    )
    features = [_scene_to_geojson_feature(s) for s in records if _publishable(s)]
    return {
        "type": "FeatureCollection",
        "features": features,
        "number_returned": len(features),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if len(records) == limit else None,
    }


@router.get("/{scene_id}", response_model=dict[str, Any])
def get_scene(scene_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    scene = SceneRepository.get_by_id(session, scene_id) or SceneRepository.get_by_product_id(
        session, scene_id
    )
    if (
        scene is None
        or not _publishable(scene)
        or scene.footprint_wgs84 is None
        or not scene_intersects_aoi(to_shape(scene.footprint_wgs84))
    ):
        raise HTTPException(404, "Scene not found in the NL study area.")
    return _scene_to_geojson_feature(scene)
