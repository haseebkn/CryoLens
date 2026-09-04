"""International Ice Patrol (IIP) ground truth ingestion client."""

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from cryolens.db.repositories import IIPSightingRepository

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

        Expected columns (case-insensitive, can vary slightly by year):
        - SIGHTING_DATE: mm/dd/yyyy or yyyy-mm-dd
        - SIGHTING_TIME: hh:mm (UTC)
        - LATITUDE: decimal degrees
        - LONGITUDE: decimal degrees
        - SIZE: size class string (e.g., 'Small', 'Medium')
        - SHAPE: shape string (e.g., 'Tabular', 'Non-Tabular')
        """
        csv_path = Path(csv_path)
        if not csv_path.is_file():
            raise FileNotFoundError(f"IIP CSV not found: {csv_path}")

        count = 0
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            # Normalize field names to uppercase for robust matching
            if reader.fieldnames:
                fieldnames = [str(name).strip().upper() for name in reader.fieldnames]
                reader.fieldnames = fieldnames

            for row in reader:
                try:
                    # 1. Parse Date and Time
                    date_str = row.get("SIGHTING_DATE") or row.get("DATE")
                    time_str = row.get("SIGHTING_TIME") or row.get("TIME")

                    if not date_str or not time_str:
                        logger.warning(f"Skipping row missing date/time: {row}")
                        continue

                    # Normalize time_str
                    time_str = time_str.zfill(4)  # Ensure at least 4 digits

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
                    lat = float(row.get("LATITUDE", 0))
                    lon = float(row.get("LONGITUDE", 0))

                    # Ensure western longitudes are negative
                    if lon > 0 and lon > 30 and lon < 80:
                        lon = -lon

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
                except Exception as e:
                    logger.error(f"Failed to parse row: {row}. Error: {e}")

        session.commit()
        logger.info(f"Successfully ingested {count} IIP sightings from {csv_path.name}")
        return count
