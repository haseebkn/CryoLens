"""Pydantic and GeoJSON schemas for CryoLens REST API."""

from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HealthResponse(BaseModel):
    status: str = "healthy"
    database: str
    postgis_version: str | None = None
    version: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SceneProperties(BaseModel):
    id: str
    product_id: str
    platform: str
    mode: str
    polarizations: list[str]
    acquisition_time: datetime
    status: str
    raster_available: bool = False
    detection_count: int = 0
    processing_provenance: dict[str, Any] = Field(default_factory=dict)


class DetectionProperties(BaseModel):
    id: str
    scene_id: str
    confidence: float | None = None
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
    """A human review with attributable evidence, not detector-derived ground truth."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    analyst_verdict: Literal[
        "CONFIRMED_ICEBERG", "REJECTED_CLUTTER", "VESSEL", "OFFSHORE_STRUCTURE", "SEA_ICE"
    ]
    corrected_class: (
        Literal["iceberg", "ship", "offshore_structure", "sea_ice_feature", "clutter"] | None
    ) = None
    analyst_id: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str = Field(
        min_length=10,
        max_length=4000,
        description="Evidence and rationale for the review decision.",
    )

    @model_validator(mode="after")
    def consistent_class(self) -> "ValidationRequest":
        expected = {
            "CONFIRMED_ICEBERG": "iceberg",
            "VESSEL": "ship",
            "OFFSHORE_STRUCTURE": "offshore_structure",
            "SEA_ICE": "sea_ice_feature",
            "REJECTED_CLUTTER": "clutter",
        }[self.analyst_verdict]
        if self.corrected_class is not None and self.corrected_class != expected:
            raise ValueError("corrected_class conflicts with analyst_verdict")
        self.corrected_class = cast(
            Literal["iceberg", "ship", "offshore_structure", "sea_ice_feature", "clutter"], expected
        )
        return self


class ValidationResponse(BaseModel):
    id: str
    detection_id: str
    analyst_verdict: str
    corrected_class: str | None = None
    analyst_id: str | None = None
    validated_at: datetime
