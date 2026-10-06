"""Versioned research pairing assumptions; no automatic target identity."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PairingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: str = "s1-s2-review-v1"
    search_hours: float = Field(default=12, gt=0, le=24)
    motion_speed_m_s: float = Field(default=0.5, ge=0, le=5)
    sar_geolocation_allowance_m: float = Field(default=100, gt=0)
    optical_geolocation_allowance_m: float = Field(default=20, gt=0)
    minimum_review_radius_m: float = Field(default=1000, ge=100)
    maximum_review_radius_m: float = Field(default=6000, ge=1000, le=6000)
    review_grid_spacing_m: float = Field(default=10, ge=10)
    cloud_buffer_m: float = Field(default=60, ge=0)
    useful_visible_fraction: float = Field(default=0.8, gt=0, le=1)
    max_items_per_candidate: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def ordered_radii(self) -> "PairingPolicy":
        if self.minimum_review_radius_m > self.maximum_review_radius_m:
            raise ValueError("Minimum review radius exceeds the maximum")
        return self

    def matching_radius(self, seconds: float, target_extent_m: float = 0) -> float:
        return (
            self.sar_geolocation_allowance_m
            + self.optical_geolocation_allowance_m
            + max(0, target_extent_m) / 2
            + self.motion_speed_m_s * abs(seconds)
        )
