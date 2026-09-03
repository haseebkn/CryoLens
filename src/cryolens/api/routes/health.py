"""Health check endpoint for CryoLens API."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from cryolens.api.schemas import HealthResponse
from cryolens.db.session import get_db_session

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
def get_health(session: Session = Depends(get_db_session)) -> HealthResponse:
    """Check API, database connectivity, and PostGIS extension status."""
    db_status = "connected"
    postgis_ver = None
    try:
        # Check basic connectivity
        session.execute(text("SELECT 1"))
        # Check PostGIS extension
        res = session.execute(text("SELECT postgis_version();")).scalar()
        postgis_ver = str(res) if res else "unknown"
    except Exception as exc:
        db_status = f"error: {exc}"

    return HealthResponse(
        status="healthy" if db_status == "connected" else "degraded",
        database=db_status,
        postgis_version=postgis_ver,
        version="0.1.0",
        timestamp=datetime.now(UTC),
    )
