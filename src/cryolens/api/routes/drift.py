"""Explicitly unavailable drift service until forcing and model validation exist."""

from typing import NoReturn

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/drift", tags=["Drift — unavailable"])


@router.get("/{detection_id}", response_model=None)
def get_drift_trajectory(detection_id: str) -> NoReturn:
    """Do not publish legacy trajectories derived from synthetic fallback forcing."""
    raise HTTPException(
        501,
        "Drift forecasting is unavailable: physical forcing, iceberg dimensions and forecast skill have not been validated.",
    )
