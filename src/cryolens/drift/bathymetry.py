"""Reserved interface for verified research bathymetry."""

from pathlib import Path
from typing import Any


class BathymetryManager:
    """CHS NONNA reader integration is not implemented."""

    def __init__(self, data_dir: Path | str = "./data/bathymetry") -> None:
        self.data_dir = Path(data_dir)

    def attach_bathymetry(self, drift_model: Any) -> None:
        """Reject the former invented constant-depth fallback."""
        raise NotImplementedError("Validated bathymetry readers are not implemented")
