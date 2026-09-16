"""Orbit state file (POEORB/RESORB) acquisition, validation, and provenance tracking."""

import io
import logging
import re
import warnings
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

#: Public mirrors that serve official ESA orbit products without credentials.
#: Both are read-only auxiliary-data hosts, so acquiring an orbit does not
#: require the CDSE or Earthdata accounts the imagery download routes need.
ESA_STEP_AUXDATA = "https://step.esa.int/auxdata/orbits/Sentinel-1/"
ASF_AUX_POEORB = "https://s1qc.asf.alaska.edu/aux_poeorb/"
ASF_AUX_RESORB = "https://s1qc.asf.alaska.edu/aux_resorb/"

#: Validity window encoded in every EOF filename, e.g.
#: ``S1B_OPER_AUX_POEORB_OPOD_20180510T110554_V20180419T225942_20180421T005942.EOF``
_EOF_NAME_RE = re.compile(
    r"(?P<platform>S1[ABCD])_OPER_AUX_(?P<kind>POEORB|RESORB)_OPOD_"
    r"(?P<generated>\d{8}T\d{6})_"
    r"V(?P<start>\d{8}T\d{6})_(?P<stop>\d{8}T\d{6})\.EOF"
)

_EOF_TIME_FORMAT = "%Y%m%dT%H%M%S"


class OrbitType(StrEnum):
    """Sentinel-1 Precise vs Restituted Orbit file classification."""

    POEORB = "POEORB"  # Precise Orbit Ephemerides (~21 days lag, highest accuracy <5cm)
    RESORB = "RESORB"  # Restituted Orbit (~3 hours lag, operational accuracy ~10cm)


def parse_eof_name(name: str) -> dict[str, Any] | None:
    """Parse an orbit filename into platform, type and validity window.

    Returns ``None`` when the name is not a recognised EOF product, so callers
    can skip directory-listing noise without raising.
    """
    match = _EOF_NAME_RE.search(name)
    if match is None:
        return None
    start = datetime.strptime(match.group("start"), _EOF_TIME_FORMAT).replace(tzinfo=UTC)
    stop = datetime.strptime(match.group("stop"), _EOF_TIME_FORMAT).replace(tzinfo=UTC)
    return {
        "filename": match.group(0),
        "platform": match.group("platform"),
        "orbit_type": match.group("kind"),
        "generated": datetime.strptime(match.group("generated"), _EOF_TIME_FORMAT).replace(
            tzinfo=UTC
        ),
        "validity_start": start,
        "validity_stop": stop,
    }


def covers(entry: dict[str, Any], acquisition_time: datetime) -> bool:
    """Return True when an orbit product's validity window contains the acquisition."""
    return bool(entry["validity_start"] <= acquisition_time <= entry["validity_stop"])


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

    @staticmethod
    def _platform_code(platform: str) -> str:
        """Normalise a platform label to its two-or-three character mission code."""
        codes = {
            "S1A": "S1A",
            "SENTINEL-1A": "S1A",
            "S1B": "S1B",
            "SENTINEL-1B": "S1B",
            "S1C": "S1C",
            "SENTINEL-1C": "S1C",
            "S1D": "S1D",
            "SENTINEL-1D": "S1D",
        }
        code = codes.get(platform.upper())
        if code is None:
            raise ValueError(f"Unknown Sentinel-1 platform: {platform}")
        return code

    def _candidate_listings(
        self, platform: str, acquisition_time: datetime, orbit_type: OrbitType
    ) -> list[str]:
        """Directory URLs that may contain the orbit covering the acquisition.

        The ESA mirror is partitioned by the orbit product's *validity* month, so
        an acquisition near a month boundary can be served by the neighbouring
        directory. Both are tried.
        """
        urls: list[str] = []
        for offset in (0, -1, 1):
            moment = acquisition_time + timedelta(days=offset)
            urls.append(
                urljoin(
                    ESA_STEP_AUXDATA,
                    f"{orbit_type.value}/{platform}/{moment:%Y}/{moment:%m}/",
                )
            )
        urls.append(ASF_AUX_POEORB if orbit_type == OrbitType.POEORB else ASF_AUX_RESORB)
        # Preserve order while removing duplicates from the day-offset expansion.
        return list(dict.fromkeys(urls))

    def download_orbit_file(
        self,
        platform: str,
        acquisition_time: datetime,
        orbit_type: OrbitType | None = None,
        timeout: float = 60.0,
    ) -> Path:
        """Download the orbit product whose validity window covers the acquisition.

        Orbit products are official ESA auxiliary data served by public mirrors,
        so this needs no CDSE or Earthdata credentials. Imagery download still
        does.

        Obtaining an orbit is **not** the same as applying one. This method only
        places a validated EOF in the cache; geolocation is still that recorded
        in the product annotation, and ``orbit_correction_applied`` stays False
        until a correction is actually performed and verified.

        Raises:
            FileNotFoundError: If no published orbit covers the acquisition.
        """
        import requests

        code = self._platform_code(platform)
        orbit_type = orbit_type or self.determine_orbit_type(acquisition_time)
        if acquisition_time.tzinfo is None:
            acquisition_time = acquisition_time.replace(tzinfo=UTC)

        for listing_url in self._candidate_listings(code, acquisition_time, orbit_type):
            try:
                response = requests.get(listing_url, timeout=timeout)
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - mirrors fail independently
                logger.debug("Orbit listing unavailable at %s: %s", listing_url, exc)
                continue

            best: dict[str, Any] | None = None
            for raw_name in set(re.findall(r"[\w.\-]+\.EOF(?:\.zip)?", response.text)):
                entry = parse_eof_name(raw_name)
                if entry is None or entry["platform"] != code:
                    continue
                if entry["orbit_type"] != orbit_type.value or not covers(entry, acquisition_time):
                    continue
                entry["href"] = raw_name
                # Prefer the most recently generated product for the same window.
                if best is None or entry["generated"] > best["generated"]:
                    best = entry

            if best is None:
                continue

            destination = self.cache_dir / str(best["filename"])
            if destination.is_file():
                logger.info("Orbit already cached: %s", destination.name)
                return destination

            file_url = urljoin(listing_url, str(best["href"]))
            logger.info("Downloading %s orbit from %s", orbit_type.value, file_url)
            payload = requests.get(file_url, timeout=timeout)
            payload.raise_for_status()
            content = payload.content

            if str(best["href"]).endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    members = [n for n in archive.namelist() if n.endswith(".EOF")]
                    if not members:
                        logger.warning("Archive at %s contains no EOF member", file_url)
                        continue
                    content = archive.read(members[0])

            # Validate before caching so a truncated or HTML error page can never
            # masquerade as an orbit product.
            try:
                root = ET.fromstring(content)
            except ET.ParseError as exc:
                logger.warning("Downloaded orbit is not valid XML (%s): %s", file_url, exc)
                continue
            if len(root.findall(".//OSV")) < 2:
                logger.warning("Downloaded orbit has too few state vectors: %s", file_url)
                continue

            destination.write_bytes(content)
            logger.info(
                "Cached orbit %s (%d state vectors)", destination.name, len(root.findall(".//OSV"))
            )
            return destination

        raise FileNotFoundError(
            f"No published {orbit_type.value} orbit covers {code} at "
            f"{acquisition_time.isoformat()} on any configured public mirror."
        )

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
