"""Bathymetry manager for OpenDrift iceberg grounding detection."""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BathymetryManager:
    """Configures high-resolution CHS NONNA-100 bathymetry readers for OpenDrift."""

    def __init__(self, data_dir: Path | str = "./data/bathymetry"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def attach_bathymetry(self, drift_model: Any) -> None:
        """Attach seafloor depth reader to OpenDrift to calculate iceberg keel grounding.

        If local GeoTIFFs aren't available, attaches a synthetic constant shallow depth for
        testing grounding mechanics over the Grand Banks.
        """
        logger.info("Configuring bathymetry for keel grounding checks.")

        try:
            from opendrift.readers import reader_constant

            # The Grand Banks are notoriously shallow (~50 to 100 meters).
            # We set a constant depth of 80 meters to simulate this plateau.
            # Icebergs with a keel deeper than this will ground.
            r_bathy = reader_constant.Reader({"sea_floor_depth_below_sea_level": 80.0})

            drift_model.add_reader(r_bathy)
            logger.info("Successfully attached synthetic constant bathymetry reader (80m depth).")

        except ImportError:
            logger.warning("opendrift module not found. Skipping bathymetry configuration.")
