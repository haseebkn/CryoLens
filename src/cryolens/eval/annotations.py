"""Annotation contracts and acquisition-local one-to-one reference matching."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Geod
from scipy.optimize import linear_sum_assignment

from cryolens.eval.cohort import Task

LABELS = {
    "target_detection": {"target", "non_target", "uncertain", "unreviewed"},
    "sea_ice_segmentation": {"open_water", "ice_affected", "unknown"},
    "target_identification": {"ship", "iceberg", "other", "unknown"},
}


class Evidence(BaseModel):
    """Provenance for a reviewed observation, never a fabricated identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["sar_review", "optical", "ais", "field_observation", "iip_context"]
    reference: str = Field(min_length=1)
    acquired_utc: datetime
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def aware(self) -> Self:
        if self.acquired_utc.tzinfo is None:
            raise ValueError("Evidence timestamps need timezones")
        return self


class Annotation(BaseModel):
    """A review record; separate task labels prevent target/identity conflation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scene_id: str = Field(min_length=1)
    task: Task
    label: str
    reviewer_id: str = Field(min_length=1)
    reviewed_utc: datetime
    acquired_utc: datetime
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    evidence: list[Evidence]
    surveyed_area_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    sampling_stratum: str = Field(min_length=1)
    inclusion_probability: float = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.label not in LABELS[self.task]:
            raise ValueError("Label does not belong to this task")
        if self.reviewed_utc.tzinfo is None or self.acquired_utc.tzinfo is None:
            raise ValueError("Annotation timestamps need timezones")
        unresolved = self.label in {"unknown", "uncertain", "unreviewed"}
        if not unresolved and not self.evidence:
            raise ValueError("Resolved labels need recorded evidence")
        if self.task == "target_identification" and not unresolved:
            independent = [
                e for e in self.evidence if e.kind in {"optical", "ais", "field_observation"}
            ]
            if not independent:
                raise ValueError(
                    "Identity needs independent corroboration; SAR/IIP alone is insufficient"
                )
            for evidence in independent:
                if abs((evidence.acquired_utc - self.acquired_utc).total_seconds()) > 6 * 3600:
                    raise ValueError("Identity evidence falls outside the frozen six-hour window")
        return self


class ReferencePoint(BaseModel):
    """A resolved target or prediction in the same surveyed acquisition."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    point_id: str = Field(min_length=1)
    scene_id: str = Field(min_length=1)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)


def match_targets(
    predictions: list[ReferencePoint],
    references: list[ReferencePoint],
    distance_m: float = 200,
) -> list[tuple[str, str, float]]:
    """Maximum-cardinality, minimum-distance assignment, with duplicate penalties.

    Caller supplies resolved targets in independently surveyed eligible areas.
    Unknown regions cannot be converted to negative references by this helper.
    """
    if not np.isfinite(distance_m) or distance_m <= 0:
        raise ValueError("Matching distance must be positive and finite")
    for values in (predictions, references):
        if len({p.point_id for p in values}) != len(values):
            raise ValueError("Duplicate point identifiers")
    if not predictions or not references:
        return []
    geod = Geod(ellps="WGS84")
    distances = np.full((len(predictions), len(references)), np.inf)
    for i, prediction in enumerate(predictions):
        for j, reference in enumerate(references):
            if prediction.scene_id == reference.scene_id:
                distances[i, j] = abs(
                    geod.inv(
                        prediction.longitude,
                        prediction.latitude,
                        reference.longitude,
                        reference.latitude,
                    )[2]
                )
    penalty = len(predictions) + len(references) + 1.0
    cost = np.full((len(predictions), len(references) + len(predictions)), penalty)
    cost[:, : len(references)] = np.where(
        distances <= distance_m, distances / distance_m, penalty * 2
    )
    rows, columns = linear_sum_assignment(cost)
    return [
        (predictions[i].point_id, references[j].point_id, float(distances[i, j]))
        for i, j in zip(rows, columns, strict=True)
        if j < len(references) and distances[i, j] <= distance_m
    ]
