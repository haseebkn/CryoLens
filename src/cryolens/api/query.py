"""Shared bounded spatial and temporal API input validation."""

import math
from datetime import UTC, datetime

from fastapi import HTTPException


def parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    """Validate finite WGS84 bounds; antimeridian wrapping is outside this study."""
    if value is None:
        return None
    try:
        west, south, east, north = (float(part.strip()) for part in value.split(","))
        if not all(math.isfinite(v) for v in (west, south, east, north)):
            raise ValueError
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            400, "bbox must be finite, ordered WGS84 west,south,east,north bounds."
        ) from exc
    return west, south, east, north


def validate_dates(start: datetime | None, end: datetime | None) -> None:
    """Require explicit UTC offsets and chronological query bounds."""
    for value in (start, end):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(400, "Timestamps must include a UTC offset, for example Z.")
    if start is not None and end is not None and start.astimezone(UTC) > end.astimezone(UTC):
        raise HTTPException(400, "start_date must be on or before end_date.")
