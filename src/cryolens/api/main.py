"""FastAPI application initialization, routing, and static dashboard serving."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cryolens.api.routes import detections, drift, health, iip, scenes

app = FastAPI(
    title="CryoLens Maritime Domain Awareness API",
    description="Autonomous SAR iceberg detection, validation, and drift forecasting API for the Grand Banks & NE Newfoundland Shelf.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(health.router)
app.include_router(scenes.router, prefix="/api/v1")
app.include_router(detections.router, prefix="/api/v1")
app.include_router(iip.router, prefix="/api/v1")
app.include_router(drift.router, prefix="/api/v1")

# Static files directory for web dashboard
STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        """Serve the interactive Leaflet maritime dashboard."""
        return FileResponse(STATIC_DIR / "index.html")
