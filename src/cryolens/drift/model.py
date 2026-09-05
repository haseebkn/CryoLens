"""Drift interface reserved until real forcing and forecast skill are validated."""

from typing import Any

from cryolens.db.models import DetectionModel

FORECAST_UNAVAILABLE = (
    "Drift forecasting is unavailable: real time-matched ocean/wind forcing, "
    "bathymetry, iceberg physical parameters and forecast validation are not "
    "implemented. Installing OpenDrift alone does not enable forecasts."
)


class IcebergDriftRunner:
    """Fail closed rather than publish trajectories from invented conditions."""

    def run_forecast(self, detection: DetectionModel, hours: float = 72.0) -> list[dict[str, Any]]:
        """No operational or experimental forecast is currently supported."""
        raise NotImplementedError(FORECAST_UNAVAILABLE)
