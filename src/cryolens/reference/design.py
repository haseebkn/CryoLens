"""Frozen stratified candidate sampling; selection probabilities are explicit."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import hypergeom

from cryolens.eval.cohort import digest


class ReferencePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_id: str
    schema_version: int = 1
    parent_protocol: str
    seed: int = Field(ge=0)
    survey_core_pixels: int = Field(ge=32, le=1024)
    survey_context_pixels: int = Field(ge=0, le=128)
    candidate_context_pixels: int = Field(ge=16, le=256)
    cfar_core_pixels: int = Field(ge=32, le=1024)
    min_size_samples_per_stratum: int = Field(ge=2)
    other_rejection_samples_per_stratum: int = Field(ge=2)
    repeat_fraction: float = Field(gt=0, le=1)
    repeat_delay_days: int = Field(ge=7)
    uncertain_buffer_pixels: int = Field(ge=1)
    brightness_edges_db: tuple[float, float]
    scope: str
    strata: str
    independence: str
    blinding: str
    area: str
    identity: str
    reporting: str


def sample_candidates(
    frame: list[dict[str, Any]], policy: ReferencePolicy
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Census retained items and SRSWOR rejected items, with immutable frame counts."""
    if len({c["id"] for c in frame}) != len(frame):
        raise ValueError("Duplicate frame candidate identifiers")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    low, high = policy.brightness_edges_db
    if not np.isfinite([low, high]).all() or low >= high:
        raise ValueError("Brightness strata need finite ordered edges")
    for candidate in frame:
        stage = candidate["first_rejection"] or "retained"
        pixels = candidate["pixel_area"]
        if pixels < 1 or not np.isfinite(candidate["peak_hv_db"]):
            raise ValueError("Invalid sampling-frame component")
        size = str(pixels) if pixels <= 3 else "4-15" if pixels < 16 else "16+"
        brightness = (
            "low"
            if candidate["peak_hv_db"] < low
            else "mid"
            if candidate["peak_hv_db"] < high
            else "high"
        )
        stratum = f"{candidate['scene_id']}|{stage}|{size}|{brightness}"
        groups[stratum].append(candidate)
    selected, strata = [], []
    for stratum, values in sorted(groups.items()):
        values = sorted(values, key=lambda c: c["id"])
        stage = values[0]["first_rejection"] or "retained"
        budget = (
            len(values)
            if stage == "retained"
            else policy.min_size_samples_per_stratum
            if stage == "min_size"
            else policy.other_rejection_samples_per_stratum
        )
        count = min(budget, len(values))
        # Independent RNG per stratum avoids changing earlier selections when a new stratum appears.
        seed = int(digest({"seed": policy.seed, "stratum": stratum})[:16], 16)
        indices = np.random.default_rng(seed).choice(len(values), count, replace=False)
        probability = count / len(values)
        for index in indices:
            selected.append(
                {
                    **values[int(index)],
                    "sampling_stratum": stratum,
                    "inclusion_probability": probability,
                    "population_count": len(values),
                    "selected_count": count,
                }
            )
        strata.append(
            {
                "stratum": stratum,
                "first_rejection": stage,
                "population_count": len(values),
                "selected_count": count,
                "inclusion_probability": probability,
                "method": "census" if count == len(values) else "simple_random_without_replacement",
            }
        )
    return sorted(selected, key=lambda c: digest({"seed": policy.seed, "id": c["id"]})), strata


def finite_population_interval(
    population: int, sample: int, positives: int, confidence: float = 0.95
) -> tuple[int, int]:
    """Invert hypergeometric tails for a conditional SRSWOR population count.

    This accounts for finite sampling only, not reviewer error or regional
    scene/group dependence. Zero sampled positives still leaves an upper bound.
    """
    if not 0 <= positives <= sample <= population or sample < 1 or not 0 < confidence < 1:
        raise ValueError("Invalid finite-population interval inputs")
    alpha = (1 - confidence) / 2
    lo, hi = positives, population - sample + positives
    lower_left, lower_right = lo, hi
    while lower_left < lower_right:
        mid = (lower_left + lower_right) // 2
        if hypergeom.sf(positives - 1, population, mid, sample) >= alpha:
            lower_right = mid
        else:
            lower_left = mid + 1
    upper_left, upper_right = lo, hi
    while upper_left < upper_right:
        mid = (upper_left + upper_right + 1) // 2
        if hypergeom.cdf(positives, population, mid, sample) >= alpha:
            upper_left = mid
        else:
            upper_right = mid - 1
    return lower_left, upper_left
