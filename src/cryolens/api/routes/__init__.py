"""API routes module."""

from cryolens.api.routes.detections import router as detections_router
from cryolens.api.routes.health import router as health_router
from cryolens.api.routes.scenes import router as scenes_router

__all__ = ["detections_router", "health_router", "scenes_router"]
