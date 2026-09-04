"""PostGIS relational database models for CryoLens."""

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from cryolens.db.session import Base

# Universal JSON type that falls back gracefully if not on PostgreSQL (e.g. SQLite tests)
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class SceneModel(Base):
    """Metadata, spatial footprint, and provenance of ingested/preprocessed SAR scenes."""

    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    polarizations: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False)
    acquisition_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    footprint_epsg3978 = mapped_column(
        Geometry(geometry_type="POLYGON", srid=3978, spatial_index=True),
        nullable=True,
    )
    footprint_wgs84 = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )
    processing_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    cog_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PROCESSED", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    detections: Mapped[list["DetectionModel"]] = relationship(
        "DetectionModel", back_populates="scene", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_scenes_acquisition_time", "acquisition_time"),
        Index("idx_scenes_status", "status"),
    )


class DetectionModel(Base):
    """SAR target detections produced by CFAR or deep learning detectors."""

    __tablename__ = "detections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    scene_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("scenes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    geom_epsg3978 = mapped_column(
        Geometry(geometry_type="POLYGON", srid=3978, spatial_index=True),
        nullable=True,
    )
    geom_wgs84 = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )
    centroid_wgs84 = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True),
        nullable=True,
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    detector_name: Mapped[str] = mapped_column(String(64), nullable=False, default="CA-CFAR")
    detector_params: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    predicted_class: Mapped[str] = mapped_column(
        String(64), nullable=False, default="iceberg", index=True
    )
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    width_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_sigma0_hv_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_sigma0_hv_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    peak_sigma0_hh_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    hh_hv_ratio_db: Mapped[float | None] = mapped_column(Float, nullable=True)
    incidence_angle_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    scene: Mapped["SceneModel"] = relationship("SceneModel", back_populates="detections")
    validations: Mapped[list["ValidationModel"]] = relationship(
        "ValidationModel", back_populates="detection", cascade="all, delete-orphan"
    )
    drift_forecasts: Mapped[list["DriftForecastModel"]] = relationship(
        "DriftForecastModel", back_populates="detection", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_detections_scene_class", "scene_id", "predicted_class"),
        Index("idx_detections_confidence", "confidence"),
    )


class ValidationModel(Base):
    """Human-in-the-loop analyst ground truth and corrections."""

    __tablename__ = "validations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    detection_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("detections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    analyst_verdict: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # e.g., "CONFIRMED_ICEBERG", "REJECTED_CLUTTER", "VESSEL", "STRUCTURE"
    corrected_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    corrected_geom_wgs84 = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )
    analyst_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    detection: Mapped["DetectionModel"] = relationship(
        "DetectionModel", back_populates="validations"
    )


class DriftForecastModel(Base):
    """Schema-only placeholder for Phase 6 physics-based drift forecasting."""

    __tablename__ = "drift_forecasts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    detection_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("detections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    forecast_init_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    geom_wgs84 = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True),
        nullable=True,
    )
    method: Mapped[str] = mapped_column(String(64), nullable=False, default="openberg")
    uncertainty_radius_m: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    detection: Mapped["DetectionModel"] = relationship(
        "DetectionModel", back_populates="drift_forecasts"
    )


class IIPSightingModel(Base):
    """International Ice Patrol (IIP) ground truth iceberg sightings."""

    __tablename__ = "iip_sightings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: str(uuid.uuid4()))
    sighting_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    geom_wgs84 = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True),
        nullable=False,
    )
    geom_epsg3978 = mapped_column(
        Geometry(geometry_type="POINT", srid=3978, spatial_index=True),
        nullable=False,
    )
    size_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shape: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="IIP")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
