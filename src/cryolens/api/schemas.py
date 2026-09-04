"""Pydantic and GeoJSON schemas for CryoLens REST API."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """System health and service connectivity status."""

    status: str = "healthy"
    database: str
    postgis_version: str | None = None
    version: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SceneProperties(BaseModel):
    """Metadata properties for SAR scene GeoJSON feature."""

    id: str
    product_id: str
    platform: str
    mode: str
    polarizations: list[str]
    acquisition_time: datetime
    status: str
    cog_path: str
    detection_count: int = 0
    processing_provenance: dict[str, Any] = Field(default_factory=dict)


class DetectionProperties(BaseModel):
    """Comprehensive radiometric, geometric, and validation properties for target detection."""

    id: str
    scene_id: str
    confidence: float
    detector_name: str
    predicted_class: str
    length_m: float | None = None
    width_m: float | None = None
    estimated_area_m2: float | None = None
    peak_sigma0_hv_db: float | None = None
    mean_sigma0_hv_db: float | None = None
    peak_sigma0_hh_db: float | None = None
    hh_hv_ratio_db: float | None = None
    incidence_angle_deg: float | None = None
    created_at: datetime
    validated: bool = False
    analyst_verdict: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class ValidationRequest(BaseModel):
    """Analyst validation submission payload."""

    analyst_verdict: Literal[
        "CONFIRMED_ICEBERG",
        "REJECTED_CLUTTER",
        "VESSEL",
        "OFFSHORE_STRUCTURE",
        "SEA_ICE",
    ]
    corrected_class: str | None = None
    analyst_id: str | None = "analyst_1"
    notes: str | None = None


class ValidationResponse(BaseModel):
    """Response confirming analyst verdict registration."""

    id: str
    detection_id: str
    analyst_verdict: str
    corrected_class: str | None = None
    analyst_id: str | None = None
    validated_at: datetime
