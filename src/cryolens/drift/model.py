"""OpenDrift openberg wrapper for physics-based iceberg drift forecasting.

Status: scaffolded, not validated. Forcing and bathymetry readers are constant
synthetic stand-ins rather than CMEMS and CHS NONNA-100, there is no residual
correction model, and nothing here has been validated against IIP resightings.
See docs/LIMITATIONS.md section 8 before relying on any output.
"""

import logging
from datetime import timedelta
from typing import Any

from cryolens.db.models import DetectionModel
from cryolens.drift.bathymetry import BathymetryManager
from cryolens.drift.forcing import ForcingManager

logger = logging.getLogger(__name__)


class IcebergDriftRunner:
    """Wrapper around OpenDrift IcebergDrift physical model."""

    def __init__(self) -> None:
        """Create the forcing and bathymetry managers."""
        self.forcing_mgr = ForcingManager()
        self.bathy_mgr = BathymetryManager()

    def run_forecast(self, detection: DetectionModel, hours: float = 72.0) -> list[dict[str, Any]]:
        """Run a drift forecast for a given SAR detection."""

        try:
            from opendrift.models.iceberg import IcebergDrift
        except ImportError as exc:
            # ADR-012: a fabricated trajectory is worse than no trajectory. It is
            # indistinguishable from a real forecast once written to the
            # database and served through the API, and drift output is the kind
            # of product someone might act on.
            raise RuntimeError(
                "OpenDrift is not installed, so no physical drift forecast can be "
                "produced. Install it and configure CMEMS and ERA5 forcing before "
                "running forecasts; see docs/LIMITATIONS.md section 8. To generate "
                "an explicitly synthetic trajectory for interface testing, call "
                "synthetic_trajectory() directly and label the output as synthetic."
            ) from exc

        # Initialize the OpenDrift Iceberg model
        o = IcebergDrift(loglevel=30)

        # Attach forcing and bathymetry
        self.forcing_mgr.attach_forcing(o)
        self.bathy_mgr.attach_bathymetry(o)

        # Extract coordinates (WGS84)
        if detection.centroid_wgs84 is not None:
            from geoalchemy2.shape import to_shape

            point = to_shape(detection.centroid_wgs84)
            lon, lat = point.x, point.y
        else:
            logger.error("Detection lacks centroid geometry. Cannot forecast.")
            return []

        # Derive physical iceberg parameters from radar dimensions
        length = detection.length_m or 100.0
        width = detection.width_m or 100.0
        # Assume a standard empirical relation for keel depth and mass if not strictly known
        keel_depth = 0.5 * length  # Rough proxy for Grand Banks tabular/pinnacle icebergs
        mass = length * width * keel_depth * 900.0  # density of ice ~900 kg/m3

        # Seed the iceberg element at the SAR detection time
        acq_time = detection.scene.acquisition_time if detection.scene else detection.created_at

        o.seed_elements(
            lon=lon,
            lat=lat,
            time=acq_time,
            mass=mass,
            length=length,
            width=width,
        )

        logger.info(f"Running OpenBerg drift forecast for {hours}h for detection {detection.id}")

        # Run forward simulation
        # Use time_step of 1 hour for output resolution
        o.run(duration=timedelta(hours=hours), time_step=timedelta(hours=1))

        # Extract trajectory
        trajectory = []
        lons = o.elements_lon()
        lats = o.elements_lat()
        times = o.get_time_array()

        # o.elements_lon() returns a 2D array: (time_steps, elements)
        # Since we seeded 1 element, we extract the first column
        for i, dt in enumerate(times[0]):
            trajectory.append(
                {
                    "lon": float(lons[i, 0]),
                    "lat": float(lats[i, 0]),
                    "time": dt,
                }
            )

        logger.info(f"Forecast complete. Generated {len(trajectory)} waypoints.")
        return trajectory

    def synthetic_trajectory(self, detection: DetectionModel, hours: float) -> list[dict[str, Any]]:
        """Generate an explicitly synthetic south-easterly drift track.

        This is **not** a forecast. It exists so the API and dashboard
        trajectory rendering can be exercised without OpenDrift installed, and
        every waypoint it returns carries ``"synthetic": True`` so the output
        can never be mistaken for physics downstream.
        """
        if detection.centroid_wgs84 is None:
            return []

        from geoalchemy2.shape import to_shape

        point = to_shape(detection.centroid_wgs84)
        lon, lat = point.x, point.y
        acq_time = detection.scene.acquisition_time if detection.scene else detection.created_at

        trajectory = []
        # A crude south-easterly track, loosely in the sense of the Labrador
        # Current. It carries no force balance, no forcing, and no grounding.
        for i in range(int(hours) + 1):
            trajectory.append(
                {
                    "lon": lon + (i * 0.01),
                    "lat": lat - (i * 0.015),
                    "time": acq_time + timedelta(hours=i),
                    "synthetic": True,
                }
            )
        return trajectory
