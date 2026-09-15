"""FastAPI research API and static dashboard serving."""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import mapping

from cryolens.api.routes import detections, drift, health, iip, scenes
from cryolens.config.settings import get_settings
from cryolens.geo.aoi import load_aoi

app = FastAPI(
    title="CryoLens Maritime Domain Awareness API",
    description="Research portfolio API for unverified SAR candidates in the Newfoundland and Labrador shelf study area. Not a navigation service or C-CORE certified product. Scores are uncalibrated; drift and AIS discrimination are unavailable.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Same-origin dashboard; cross-origin credentialed writes are not enabled.
app.include_router(health.router)
app.include_router(scenes.router, prefix="/api/v1")
app.include_router(detections.router, prefix="/api/v1")
app.include_router(iip.router, prefix="/api/v1")
app.include_router(drift.router, prefix="/api/v1")


@app.get("/api/v1/capabilities", tags=["Project"])
def capabilities() -> dict[str, Any]:
    """Expose scope and limitations without implying an operational feed."""
    settings = get_settings()
    return {
        "project_status": "research_portfolio",
        "operational_use": False,
        "scope": "Newfoundland and Labrador shelf study area; not a jurisdictional boundary",
        "aoi": {
            "type": "Feature",
            "geometry": mapping(load_aoi()),
            "properties": {"name": "NL shelf study area"},
        },
        "analyst_writes_enabled": bool(settings.analyst_api_key and settings.analyst_id.strip()),
        "confidence": "uncalibrated detector scores; no measured operational false-positive rate",
        "ais": "unavailable",
        "drift": "unavailable_unvalidated",
        "affiliation": "Independent portfolio project; no C-CORE affiliation or certification",
    }


STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        """Serve the Leaflet research dashboard."""
        return FileResponse(STATIC_DIR / "index.html")
