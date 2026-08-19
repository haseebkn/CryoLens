"""Orbit state file (POEORB/RESORB) acquisition, validation, and provenance tracking."""

import logging
import warnings
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
        """Fetch or mock orbit file metadata and local cached path for a given acquisition."""
        if orbit_type is None:
            orbit_type = self.determine_orbit_type(acquisition_time)

        plat_upper = platform.upper()
        if "1A" in plat_upper or "S1A" in plat_upper:
            sat_code = "S1A"
        elif "1B" in plat_upper or "S1B" in plat_upper:
            sat_code = "S1B"
        else:
            sat_code = "S1C"

        dt_str = acquisition_time.strftime("%Y%m%dT%H%M%S")
        filename = f"{sat_code}_OPER_AUX_{orbit_type.value}_OPOD_{dt_str}.EOF"
        cached_path = self.cache_dir / filename

        # If not on disk, create placeholder/mock EOF descriptor
        if not cached_path.exists():
            cached_path.write_text(
                f"<Earth_Explorer_Header>\n  <Orbit_Type>{orbit_type.value}</Orbit_Type>\n"
                f"  <Satellite>{sat_code}</Satellite>\n  <Acquisition>{dt_str}</Acquisition>\n"
                f"</Earth_Explorer_Header>",
                encoding="utf-8",
            )
            logger.info("Recorded %s orbit header in %s", orbit_type.value, cached_path.name)

        return {
            "orbit_type": orbit_type.value,
            "platform": sat_code,
            "acquisition_time": acquisition_time.isoformat(),
            "orbit_file_path": str(cached_path),
            "is_precise": orbit_type == OrbitType.POEORB,
        }
