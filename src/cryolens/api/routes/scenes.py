"""Scene query and management endpoints."""

from datetime import datetime
from typing import Any

import shapely.geometry
from fastapi import APIRouter, Depends, HTTPException, Query
from geoalchemy2.shape import to_shape
from sqlalchemy.orm import Session

from cryolens.db.models import SceneModel
from cryolens.db.repositories import SceneRepository
from cryolens.db.session import get_db_session

router = APIRouter(prefix="/scenes", tags=["Scenes"])


def _scene_to_geojson_feature(scene: SceneModel) -> dict[str, Any]:
    """Serialize SceneModel to a GeoJSON Feature dict."""
    geom_dict = None
    if scene.footprint_wgs84 is not None:
        shape = to_shape(scene.footprint_wgs84)
        geom_dict = shapely.geometry.mapping(shape)

    return {
        "type": "Feature",
        "id": scene.id,
        "geometry": geom_dict,
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
            "cog_path": scene.cog_path,
            "detection_count": len(scene.detections) if scene.detections else 0,
            "processing_provenance": scene.processing_provenance,
            "created_at": scene.created_at.isoformat() if scene.created_at else None,
        },
    }


@router.get("", response_model=dict[str, Any])
def list_scenes(
    start_date: datetime | None = Query(
        default=None, description="Filter scenes acquired on/after this timestamp"
    ),
    end_date: datetime | None = Query(
        default=None, description="Filter scenes acquired on/before this timestamp"
    ),
    status: str | None = Query(default=None, description="Filter by status (PROCESSED, DETECTED)"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve scenes as a GeoJSON FeatureCollection."""
    scenes = SceneRepository.list_scenes(
        session=session,
        start_date=start_date,
        end_date=end_date,
        status=status,
        limit=limit,
        offset=offset,
    )
    features = [_scene_to_geojson_feature(s) for s in scenes]
    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/{scene_id}", response_model=dict[str, Any])
def get_scene(
    scene_id: str,
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Retrieve single scene details and footprint."""
    scene = SceneRepository.get_by_id(session, scene_id)
    if scene is None:
        scene = SceneRepository.get_by_product_id(session, scene_id)
    if scene is None:
        raise HTTPException(status_code=404, detail=f"Scene '{scene_id}' not found.")

    return _scene_to_geojson_feature(scene)
