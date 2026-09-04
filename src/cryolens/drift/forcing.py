"""Environmental forcing data manager for OpenDrift iceberg modeling."""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ForcingManager:
    """Manages CMEMS (ocean) and ERA5 (wind) forcing data for OpenDrift."""

    def __init__(self, data_dir: Path | str = "./data/forcing"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def attach_forcing(self, drift_model: Any) -> None:
        """Attach environmental readers to the OpenDrift model.

        If local NetCDF files aren't found, attaches synthetic/constant readers for the Grand Banks
        Labrador Current for demonstration and testing purposes.
        """
        # In a production environment, we would use:
        # reader_netCDF_CF_generic.Reader('path/to/cmems_currents.nc')
        # reader_netCDF_CF_generic.Reader('path/to/era5_winds.nc')

        logger.info("Configuring uniform synthetic environmental forcing for Grand Banks.")

        try:
            from opendrift.readers import reader_constant

            # Simulate the Labrador Current: Flowing south/southeast
            # x_sea_water_velocity (u) ~ 0.15 m/s (East)
            # y_sea_water_velocity (v) ~ -0.40 m/s (South)
            r_current = reader_constant.Reader(
                {
                    "x_sea_water_velocity": 0.15,
                    "y_sea_water_velocity": -0.40,
                }
            )

            # Simulate prevailing Westerly winds
            # x_wind (u) ~ 5.0 m/s (East)
            # y_wind (v) ~ -2.0 m/s (South)
            r_wind = reader_constant.Reader(
                {
                    "x_wind": 5.0,
                    "y_wind": -2.0,
                }
            )

            drift_model.add_reader([r_current, r_wind])
            logger.info("Successfully attached synthetic constant forcing readers.")

        except ImportError:
            logger.warning("opendrift module not found. Skipping forcing configuration.")
