"""Reserved interface for verified environmental forcing."""

from pathlib import Path
from typing import Any


class ForcingManager:
    """Forcing must include source, time coverage, units and missing-data checks."""

    def __init__(self, data_dir: Path | str = "./data/forcing") -> None:
        self.data_dir = Path(data_dir)

    def attach_forcing(self, drift_model: Any) -> None:
        """Reject the former synthetic current/wind fallback."""
        raise NotImplementedError("Validated CMEMS/ERA5 forcing readers are not implemented")
