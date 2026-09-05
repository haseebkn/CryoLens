"""Orbit state file (POEORB/RESORB) acquisition, validation, and provenance tracking."""

import logging
import warnings
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class OrbitType(StrEnum):
    """Sentinel-1 Precise vs Restituted Orbit file classification."""

    POEORB = "POEORB"  # Precise Orbit Ephemerides (~21 days lag, highest accuracy <5cm)
    RESORB = "RESORB"  # Restituted Orbit (~3 hours lag, operational accuracy ~10cm)


class OrbitManager:
    """Manages Sentinel-1 orbit file downloads, caching, and provenance."""

    def __init__(self, cache_dir: Path | str = "./data/cache/orbits") -> None:
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def determine_orbit_type(
        self,
        acquisition_time: datetime,
        preference: str = "POEORB",
    ) -> OrbitType:
        """Determine whether POEORB is available based on acquisition age (~21 days lag)."""
        now = datetime.now(UTC)
        if acquisition_time.tzinfo is None:
            acquisition_time = acquisition_time.replace(tzinfo=UTC)

        age = now - acquisition_time
        poeorb_available = age >= timedelta(days=21)

        if preference.upper() == "POEORB":
            if poeorb_available:
                return OrbitType.POEORB
            else:
                warnings.warn(
                    f"POEORB orbit file is not yet available for scene acquired {acquisition_time.strftime('%Y-%m-%d')} "
                    f"({age.days} days old < 21-day latency). Falling back to RESORB restituted orbit.",
                    category=UserWarning,
                    stacklevel=2,
                )
                logger.warning(
                    "Scene is %d days old (<21 days). Using RESORB orbit instead of POEORB.",
                    age.days,
                )
                return OrbitType.RESORB

        return OrbitType.RESORB

    def get_orbit_file(
        self,
        platform: str,
        acquisition_time: datetime,
        orbit_type: OrbitType | None = None,
    ) -> dict[str, Any]:
        """Find a real cached EOF with state vectors covering the acquisition.

        Availability is not inferred from age. This reader does not download or
        apply orbit corrections; callers must record application separately.
        """
        if acquisition_time.tzinfo is None:
            acquisition_time = acquisition_time.replace(tzinfo=UTC)
        orbit_type = orbit_type or self.determine_orbit_type(acquisition_time)
        codes = {
            "S1A": "S1A",
            "SENTINEL-1A": "S1A",
            "S1B": "S1B",
            "SENTINEL-1B": "S1B",
            "S1C": "S1C",
            "SENTINEL-1C": "S1C",
        }
        sat_code = codes.get(platform.upper())
        if sat_code is None:
            raise ValueError(f"Unknown Sentinel-1 platform: {platform}")
        for path in sorted(
            self.cache_dir.glob(f"{sat_code}_*{orbit_type.value}*.EOF"), reverse=True
        ):
            try:
                root = ET.parse(path).getroot()
                mission = root.findtext(".//Mission", "").upper().replace("SENTINEL-", "S")
                file_type = root.findtext(".//File_Type", "")
                if mission != sat_code or file_type != f"AUX_{orbit_type.value}":
                    continue
                vectors = root.findall(".//OSV")
                times = []
                for vector in vectors:
                    utc = vector.findtext("UTC", "").removeprefix("UTC=")
                    dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
                    times.append(dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt)
                    for tag in ("X", "Y", "Z", "VX", "VY", "VZ"):
                        import math

                        if not math.isfinite(float(vector.findtext(tag, "nan"))):
                            raise ValueError("Invalid orbit state vector")
                if len(times) < 2 or not min(times) <= acquisition_time <= max(times):
                    continue
            except (ET.ParseError, ValueError, OSError):
                logger.warning("Ignoring invalid or incomplete orbit file %s", path.name)
                continue
            return {
                "orbit_type": orbit_type.value,
                "platform": sat_code,
                "acquisition_time": acquisition_time.isoformat(),
                "orbit_file_path": str(path),
                "is_precise": orbit_type == OrbitType.POEORB,
                "orbit_correction_applied": False,
            }
        raise FileNotFoundError(
            f"No valid cached {orbit_type.value} orbit covers {sat_code} at {acquisition_time.isoformat()}. "
            "Download an official EOF into the orbit cache or use SNAP orbit retrieval."
        )
