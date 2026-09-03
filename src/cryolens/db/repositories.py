"""Data access repositories for CryoLens PostGIS entities."""

import uuid
from datetime import datetime
from typing import Any, cast

from geoalchemy2 import WKBElement
from geoalchemy2.functions import ST_GeomFromGeoJSON, ST_MakeEnvelope
from geoalchemy2.shape import from_shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from cryolens.db.models import (
    DetectionModel,
    DriftForecastModel,
    IIPSightingModel,
    SceneModel,
    ValidationModel,
)


class SceneRepository:
    """Repository handling CRUD operations for SAR scenes."""

    @staticmethod
    def create_scene(
        session: Session,
        product_id: str,
        platform: str,
        mode: str,
        polarizations: list[str],
        acquisition_time: datetime,
        cog_path: str,
        footprint_epsg3978: BaseGeometry | str | None = None,
        footprint_wgs84: BaseGeometry | str | None = None,
        processing_provenance: dict[str, Any] | None = None,
        status: str = "PROCESSED",
        scene_id: str | None = None,
    ) -> SceneModel:
        """Create and persist a new scene record."""
        geom_3978 = None
        if footprint_epsg3978 is not None:
            if isinstance(footprint_epsg3978, BaseGeometry):
                geom_3978 = from_shape(footprint_epsg3978, srid=3978)
            elif isinstance(footprint_epsg3978, str):
                geom_3978 = cast(WKBElement, ST_GeomFromGeoJSON(footprint_epsg3978))

        geom_4326 = None
        if footprint_wgs84 is not None:
            if isinstance(footprint_wgs84, BaseGeometry):
                geom_4326 = from_shape(footprint_wgs84, srid=4326)
            elif isinstance(footprint_wgs84, str):
                geom_4326 = cast(WKBElement, ST_GeomFromGeoJSON(footprint_wgs84))

        scene = SceneModel(
            id=scene_id or str(uuid.uuid4()),
            product_id=product_id,
            platform=platform,
            mode=mode,
            polarizations=polarizations,
            acquisition_time=acquisition_time,
            footprint_epsg3978=geom_3978,
            footprint_wgs84=geom_4326,
            processing_provenance=processing_provenance or {},
            cog_path=cog_path,
            status=status,
        )
        session.add(scene)
        session.flush()
        return scene

    @staticmethod
    def get_by_id(session: Session, scene_id: str) -> SceneModel | None:
        """Retrieve scene by unique ID."""
        return session.get(SceneModel, scene_id)

    @staticmethod
    def get_by_product_id(session: Session, product_id: str) -> SceneModel | None:
        """Retrieve scene by product ID."""
        stmt = select(SceneModel).where(SceneModel.product_id == product_id)
        return session.scalars(stmt).first()

    @staticmethod
    def list_scenes(
        session: Session,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SceneModel]:
        """List scenes matching temporal or status criteria."""
        stmt: Select[tuple[SceneModel]] = select(SceneModel)
        filters = []
        if start_date:
            filters.append(SceneModel.acquisition_time >= start_date)
        if end_date:
            filters.append(SceneModel.acquisition_time <= end_date)
        if status:
            filters.append(SceneModel.status == status)

        if filters:
            stmt = stmt.where(and_(*filters))

        stmt = stmt.order_by(SceneModel.acquisition_time.desc()).limit(limit).offset(offset)
        return list(session.scalars(stmt).all())


class DetectionRepository:
    """Repository handling CRUD and spatial filtering for iceberg/ship detections."""

    @staticmethod
    def create_detection(
        session: Session,
        scene_id: str,
        confidence: float,
        detector_name: str,
        predicted_class: str = "iceberg",
        geom_epsg3978: BaseGeometry | None = None,
        geom_wgs84: BaseGeometry | None = None,
        centroid_wgs84: BaseGeometry | None = None,
        length_m: float | None = None,
        width_m: float | None = None,
        estimated_area_m2: float | None = None,
        peak_sigma0_hv_db: float | None = None,
        mean_sigma0_hv_db: float | None = None,
        peak_sigma0_hh_db: float | None = None,
        hh_hv_ratio_db: float | None = None,
        incidence_angle_deg: float | None = None,
        detector_params: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
        detection_id: str | None = None,
    ) -> DetectionModel:
        """Create and persist a single detection."""
        g_3978 = from_shape(geom_epsg3978, srid=3978) if geom_epsg3978 else None
        g_4326 = from_shape(geom_wgs84, srid=4326) if geom_wgs84 else None
        c_4326 = from_shape(centroid_wgs84, srid=4326) if centroid_wgs84 else None

        detection = DetectionModel(
            id=detection_id or str(uuid.uuid4()),
            scene_id=scene_id,
            geom_epsg3978=g_3978,
            geom_wgs84=g_4326,
            centroid_wgs84=c_4326,
            confidence=confidence,
            detector_name=detector_name,
            detector_params=detector_params or {},
            predicted_class=predicted_class,
            length_m=length_m,
            width_m=width_m,
            estimated_area_m2=estimated_area_m2,
            peak_sigma0_hv_db=peak_sigma0_hv_db,
            mean_sigma0_hv_db=mean_sigma0_hv_db,
            peak_sigma0_hh_db=peak_sigma0_hh_db,
            hh_hv_ratio_db=hh_hv_ratio_db,
            incidence_angle_deg=incidence_angle_deg,
            properties=properties or {},
        )
        session.add(detection)
        session.flush()
        return detection

    @staticmethod
    def get_by_id(session: Session, detection_id: str) -> DetectionModel | None:
        """Retrieve detection by ID."""
        return session.get(DetectionModel, detection_id)

    @staticmethod
    def list_detections(
        session: Session,
        bbox: tuple[float, float, float, float] | None = None,
        scene_id: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        min_confidence: float = 0.0,
        predicted_class: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[DetectionModel]:
        """Query detections with spatial bbox (min_lon, min_lat, max_lon, max_lat) and attribute filters."""
        stmt: Select[tuple[DetectionModel]] = select(DetectionModel)
        filters = []

        if scene_id:
            filters.append(DetectionModel.scene_id == scene_id)

        if min_confidence > 0.0:
            filters.append(DetectionModel.confidence >= min_confidence)

        if predicted_class:
            filters.append(DetectionModel.predicted_class == predicted_class)

        if start_date or end_date:
            stmt = stmt.join(SceneModel, DetectionModel.scene_id == SceneModel.id)
            if start_date:
                filters.append(SceneModel.acquisition_time >= start_date)
            if end_date:
                filters.append(SceneModel.acquisition_time <= end_date)

        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            envelope = ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
            filters.append(func.ST_Intersects(DetectionModel.centroid_wgs84, envelope))

        if filters:
            stmt = stmt.where(and_(*filters))

        stmt = stmt.order_by(DetectionModel.created_at.desc()).limit(limit).offset(offset)
        return list(session.scalars(stmt).all())

    @staticmethod
    def record_validation(
        session: Session,
        detection_id: str,
        analyst_verdict: str,
        corrected_class: str | None = None,
        corrected_geom_wgs84: BaseGeometry | None = None,
        analyst_id: str | None = None,
        notes: str | None = None,
    ) -> ValidationModel:
        """Create and commit an analyst validation verdict."""
        c_geom = (
            from_shape(corrected_geom_wgs84, srid=4326)
            if corrected_geom_wgs84 is not None
            else None
        )

        validation = ValidationModel(
            id=str(uuid.uuid4()),
            detection_id=detection_id,
            analyst_verdict=analyst_verdict,
            corrected_class=corrected_class,
            corrected_geom_wgs84=c_geom,
            analyst_id=analyst_id,
            notes=notes,
        )
        session.add(validation)
        session.flush()
        return validation


class IIPSightingRepository:
    """Repository handling CRUD operations for IIP iceberg sightings."""

    @staticmethod
    def create_sighting(
        session: Session,
        sighting_time: datetime,
        lon: float,
        lat: float,
        size_class: str | None = None,
        shape: str | None = None,
        source: str = "IIP",
    ) -> IIPSightingModel:
        """Create a new IIP sighting with automatically populated CRS geometries."""
        pt_wgs84 = f"SRID=4326;POINT({lon} {lat})"

        sighting = IIPSightingModel(
            sighting_time=sighting_time,
            geom_wgs84=pt_wgs84,
            geom_epsg3978=func.ST_Transform(func.ST_GeomFromText(f"POINT({lon} {lat})", 4326), 3978),
            size_class=size_class,
            shape=shape,
            source=source,
        )
        session.add(sighting)
        session.flush()
        return sighting

    @staticmethod
    def list_sightings(
        session: Session,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[IIPSightingModel]:
        """Query sightings with temporal and spatial filters."""
        stmt: Select[tuple[IIPSightingModel]] = select(IIPSightingModel)
        filters = []

        if start_date:
            filters.append(IIPSightingModel.sighting_time >= start_date)
        if end_date:
            filters.append(IIPSightingModel.sighting_time <= end_date)

        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            envelope = ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
            filters.append(func.ST_Intersects(IIPSightingModel.geom_wgs84, envelope))

        if filters:
            stmt = stmt.where(and_(*filters))

        stmt = stmt.order_by(IIPSightingModel.sighting_time.desc()).limit(limit).offset(offset)
        return list(session.scalars(stmt).all())

    @staticmethod
    def get_sightings_in_timerange(
        session: Session, start_time: datetime, end_time: datetime
    ) -> list[IIPSightingModel]:
        """Fetch all IIP sightings within a specific temporal window."""
        return (
            session.query(IIPSightingModel)
            .filter(IIPSightingModel.sighting_time >= start_time)
            .filter(IIPSightingModel.sighting_time <= end_time)
            .all()
        )

class DriftForecastRepository:
    """CRUD operations for Drift Forecast trajectories."""

    @staticmethod
    def save_trajectory(
        session: Session,
        detection_id: str,
        trajectory_points: list[dict[str, Any]],
        method: str = "openberg"
    ) -> None:
        """Save a list of trajectory waypoints to the database."""
        # First, clear any existing forecasts for this detection from this method
        session.query(DriftForecastModel).filter(
            DriftForecastModel.detection_id == detection_id,
            DriftForecastModel.method == method
        ).delete()

        forecasts = []
        for pt in trajectory_points:
            lon, lat = pt["lon"], pt["lat"]
            forecast = DriftForecastModel(
                detection_id=detection_id,
                valid_time=pt["time"],
                geom_wgs84=f"SRID=4326;POINT({lon} {lat})",
                method=method,
            )
            forecasts.append(forecast)

        session.add_all(forecasts)
        session.flush()

    @staticmethod
    def get_trajectory(
        session: Session, detection_id: str, method: str = "openberg"
    ) -> list[DriftForecastModel]:
        """Get the forecast trajectory for a detection, ordered by time."""
        return (
            session.query(DriftForecastModel)
            .filter(DriftForecastModel.detection_id == detection_id)
            .filter(DriftForecastModel.method == method)
            .order_by(DriftForecastModel.valid_time.asc())
            .all()
        )
