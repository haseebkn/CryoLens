"""Cross-referencing logic for IIP ground truth and CFAR detections."""

import logging
from datetime import timedelta

from geoalchemy2.functions import ST_Distance
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from cryolens.db.models import DetectionModel, IIPSightingModel

logger = logging.getLogger(__name__)


class SpatiotemporalMatcher:
    """Matches IIP iceberg sightings with CFAR detections using a drift buffer."""

    def __init__(self, max_drift_speed_ms: float = 0.5):
        self.max_drift_speed_ms = max_drift_speed_ms

    def correlate_scene(
        self, session: Session, scene_id: str, time_window_hours: float = 24.0
    ) -> int:
        """Find IIP sightings near a scene's detections within the time window.

        Tags matching detections with `IIP_CORRELATED = True` in their properties.
        """
        # Get all detections for the scene
        stmt = select(DetectionModel).where(DetectionModel.scene_id == scene_id)
        detections = list(session.scalars(stmt).all())

        if not detections:
            logger.info(f"No detections found for scene {scene_id}. Skipping correlation.")
            return 0

        # The scene acquisition time is the same for all detections in a scene
        scene_time = detections[0].scene.acquisition_time

        # Find IIP sightings within time window
        start_time = scene_time - timedelta(hours=time_window_hours)
        end_time = scene_time + timedelta(hours=time_window_hours)

        iip_stmt = select(IIPSightingModel).where(
            and_(
                IIPSightingModel.sighting_time >= start_time,
                IIPSightingModel.sighting_time <= end_time,
            )
        )
        sightings = list(session.scalars(iip_stmt).all())

        if not sightings:
            logger.info(f"No IIP sightings found within {time_window_hours}h of scene {scene_id}.")
            return 0

        correlated_count = 0

        # Spatiotemporal matching
        for detection in detections:
            is_correlated = False
            for sighting in sightings:
                time_diff = abs((scene_time - sighting.sighting_time).total_seconds())

                # Maximum theoretical distance an iceberg could drift
                max_drift_radius_m = self.max_drift_speed_ms * time_diff

                # Minimum buffer to account for GPS error and iceberg size (e.g., 500m)
                buffer_radius_m = max(500.0, max_drift_radius_m)

                # Query distance directly using PostGIS since models are detached/lazy loaded geo sometimes
                # But here we can use ST_Distance in a query or use shapely if we load it

                # Let's run a spatial query
                dist_query = select(
                    ST_Distance(DetectionModel.geom_epsg3978, IIPSightingModel.geom_epsg3978)
                ).where(and_(DetectionModel.id == detection.id, IIPSightingModel.id == sighting.id))
                distance_m = session.scalar(dist_query)

                if distance_m is not None and distance_m <= buffer_radius_m:
                    is_correlated = True
                    break

            if is_correlated:
                # Update properties JSON
                props = dict(detection.properties)
                props["IIP_CORRELATED"] = True
                detection.properties = props
                correlated_count += 1

        session.commit()
        logger.info(
            f"Correlated {correlated_count}/{len(detections)} detections with IIP sightings."
        )
        return correlated_count
