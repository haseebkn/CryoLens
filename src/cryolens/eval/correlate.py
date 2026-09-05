"""Conservative IIP proximity associations, never automated ground-truth labels."""

import logging
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

from geoalchemy2.functions import ST_Distance
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from cryolens.db.models import DetectionModel, IIPSightingModel

logger = logging.getLogger(__name__)


def unambiguous_pairs(edges: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Keep associations unique in both directions; do not guess amid clutter."""
    unique = set(edges)
    detections = Counter(d for d, _ in unique)
    sightings = Counter(s for _, s in unique)
    return {(d, s) for d, s in unique if detections[d] == 1 and sightings[s] == 1}


def _utc(value: datetime) -> datetime:
    """SQLite omits timezone metadata; database timestamps are stored as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SpatiotemporalMatcher:
    """Record unique proximity associations for review, not precision or recall."""

    def __init__(self, max_drift_speed_ms: float = 0.5) -> None:
        if not math.isfinite(max_drift_speed_ms) or max_drift_speed_ms < 0:
            raise ValueError("max_drift_speed_ms must be finite and nonnegative")
        self.max_drift_speed_ms = max_drift_speed_ms

    def correlate_scene(
        self, session: Session, scene_id: str, time_window_hours: float = 24.0
    ) -> int:
        """Refresh association provenance; never change classification or validation."""
        if not math.isfinite(time_window_hours) or time_window_hours < 0:
            raise ValueError("time_window_hours must be finite and nonnegative")
        detections = list(
            session.scalars(select(DetectionModel).where(DetectionModel.scene_id == scene_id)).all()
        )
        if not detections:
            return 0
        scene_time = _utc(detections[0].scene.acquisition_time)
        sightings = list(
            session.scalars(
                select(IIPSightingModel).where(
                    and_(
                        IIPSightingModel.sighting_time
                        >= scene_time - timedelta(hours=time_window_hours),
                        IIPSightingModel.sighting_time
                        <= scene_time + timedelta(hours=time_window_hours),
                    )
                )
            ).all()
        )
        evidence: dict[tuple[str, str], dict[str, float | str]] = {}
        for detection in detections:
            for sighting in sightings:
                delta_s = abs((scene_time - _utc(sighting.sighting_time)).total_seconds())
                radius_m = max(500.0, self.max_drift_speed_ms * delta_s)
                distance = session.scalar(
                    select(
                        ST_Distance(
                            DetectionModel.geom_epsg3978,
                            IIPSightingModel.geom_epsg3978,
                        )
                    ).where(
                        and_(DetectionModel.id == detection.id, IIPSightingModel.id == sighting.id)
                    )
                )
                if distance is not None and math.isfinite(distance) and 0 <= distance <= radius_m:
                    evidence[(str(detection.id), str(sighting.id))] = {
                        "sighting_id": str(sighting.id),
                        "time_difference_s": delta_s,
                        "distance_m": float(distance),
                        "drift_envelope_m": radius_m,
                        "distance_method": "EPSG:3978 planar approximation",
                    }
        accepted = unambiguous_pairs(list(evidence))
        for detection in detections:
            key = str(detection.id)
            candidates = [pair for pair in evidence if pair[0] == key]
            matches = [pair for pair in accepted if pair[0] == key]
            props = dict(detection.properties or {})
            # Clear stale associations even when there are no current sightings.
            props["IIP_CORRELATED"] = bool(matches)
            props["iip_association"] = {
                "status": "unverified_proximity_association"
                if matches
                else ("ambiguous" if candidates else "no_association"),
                "candidate_sightings": len(candidates),
                "confirms_identity": False,
                "time_window_hours": time_window_hours,
                "max_drift_speed_ms": self.max_drift_speed_ms,
                "evidence": evidence[matches[0]] if matches else None,
            }
            detection.properties = props
        session.commit()
        logger.info(
            "Recorded %d unique IIP proximity associations; these are not verified iceberg labels",
            len(accepted),
        )
        return len(accepted)
