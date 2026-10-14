"""International Ice Patrol sighting context ingestion (not independent target truth)."""

import csv
import logging
import math
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from cryolens.db.repositories import IIPSightingRepository
from cryolens.geo.aoi import contains_point

logger = logging.getLogger(__name__)


class IIPClient:
    """Parses and ingests IIP iceberg sightings from CSV datasets (e.g., NSIDC G00807)."""

    def __init__(self) -> None:
        """Create an ingestion client.

        Reprojection to EPSG:3978 is performed in the database by
        ``IIPSightingRepository.create_sighting`` via PostGIS ST_Transform, so
        no client-side transformer is needed.
        """

    def ingest_csv(self, session: Session, csv_path: str | Path) -> int:
        """Parse an IIP CSV file and ingest records into the database.

        Required columns (case-insensitive, exactly one alias per field):
        - SIGHTING_DATE or DATE: mm/dd/yyyy or yyyy-mm-dd
        - SIGHTING_TIME or TIME: hh:mm or hhmm (UTC)
        - SIGHTING_LATITUDE or LATITUDE: decimal degrees
        - SIGHTING_LONGITUDE or LONGITUDE: decimal degrees

        SIZE and SHAPE are optional attributes. Header validation precedes all
        writes; malformed rows are logged and skipped, but an entirely malformed
        file raises ValueError. Valid sightings outside the NL polygon are skipped.
        """
        csv_path = Path(csv_path)
        if not csv_path.is_file():
            raise FileNotFoundError(f"IIP CSV not found: {csv_path}")

        count = 0
        malformed = 0
        parsed = 0
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            # Validate once, before any database writes. Publisher files use the
            # SIGHTING_ coordinate prefix; retain support for existing normalized CSVs.
            fieldnames = [name.strip().upper() for name in (reader.fieldnames or [])]
            if not fieldnames or len(set(fieldnames)) != len(fieldnames):
                raise ValueError("IIP CSV headers are missing or duplicated")
            reader.fieldnames = fieldnames
            columns: dict[str, str] = {}
            for field, aliases in {
                "date": ("SIGHTING_DATE", "DATE"),
                "time": ("SIGHTING_TIME", "TIME"),
                "latitude": ("SIGHTING_LATITUDE", "LATITUDE"),
                "longitude": ("SIGHTING_LONGITUDE", "LONGITUDE"),
            }.items():
                present = [alias for alias in aliases if alias in fieldnames]
                if len(present) != 1:
                    raise ValueError(f"IIP CSV header requires exactly one of {aliases}")
                columns[field] = present[0]

            for row in reader:
                try:
                    if None in row:
                        raise ValueError("More values than declared CSV columns")
                    # 1. Parse Date and Time
                    date_str = row.get(columns["date"])
                    time_str = row.get(columns["time"])

                    if not date_str or not time_str:
                        raise ValueError("Missing sighting date/time")
                    date_str = date_str.strip()

                    # Normalize time_str
                    time_str = (
                        time_str.strip().replace(":", "").zfill(4)
                    )  # Ensure at least 4 digits

                    try:
                        # Try parsing common IIP formats
                        if "/" in date_str:
                            dt = datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %H%M")
                        else:
                            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M")
                    except ValueError:
                        # Fallback parsing
                        dt = datetime.fromisoformat(f"{date_str}T{time_str[:2]}:{time_str[2:]}:00")

                    dt = dt.replace(tzinfo=UTC)

                    # 2. Parse Coordinates
                    lat = float(row[columns["latitude"]])
                    lon = float(row[columns["longitude"]])

                    # Ensure western longitudes are negative
                    if lon > 0 and lon > 30 and lon < 80:
                        lon = -lon

                    if not math.isfinite(lat) or not math.isfinite(lon):
                        raise ValueError("Non-finite sighting coordinates")
                    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                        raise ValueError("Invalid WGS84 sighting coordinates")
                    parsed += 1
                    if not contains_point(lon, lat):
                        logger.warning("Skipping outside-study-area IIP coordinates.")
                        continue

                    # 3. Attributes
                    size_class = row.get("SIZE")
                    shape = row.get("SHAPE")

                    # 4. Insert. The repository builds both CRS geometries.
                    IIPSightingRepository.create_sighting(
                        session=session,
                        sighting_time=dt,
                        lon=lon,
                        lat=lat,
                        size_class=size_class,
                        shape=shape,
                        source=f"IIP_CSV_{csv_path.name}",
                    )
                    count += 1
                except (ValueError, TypeError, KeyError) as e:
                    malformed += 1
                    logger.warning("Skipping malformed IIP CSV row %d: %s", reader.line_num, e)

        if malformed and not parsed:
            raise ValueError(
                f"IIP CSV contains no parseable sightings ({malformed} malformed rows)"
            )
        session.commit()
        logger.info(
            "Ingested %d IIP sightings from %s; %d outside NL, %d malformed rows skipped",
            count,
            csv_path.name,
            parsed - count,
            malformed,
        )
        return count
