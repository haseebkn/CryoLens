"""Fetch reproducible ERA5 metadata context; this never qualifies SAR imagery."""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from cryolens.eval.cohort import file_digest


def weather_parameters(rows: list[dict[str, Any]], start: str, end: str) -> dict[str, str]:
    return {
        "latitude": ",".join(str(round(r["overlap_centroid"][1], 5)) for r in rows),
        "longitude": ",".join(str(round(r["overlap_centroid"][0], 5)) for r in rows),
        "start_date": start,
        "end_date": end,
        "hourly": "wind_speed_10m",
        "wind_speed_unit": "ms",
        "models": "era5",
        "cell_selection": "sea",
        "elevation": ",".join("nan" for _ in rows),
        "timezone": "GMT",
    }


def wind_context(row: dict[str, Any], weather: dict[str, Any]) -> dict[str, Any] | None:
    if weather["hourly_units"]["wind_speed_10m"] != "m/s" or weather["utc_offset_seconds"] != 0:
        raise ValueError("Unexpected wind units or timezone")
    times = [datetime.fromisoformat(t).replace(tzinfo=UTC) for t in weather["hourly"]["time"]]
    winds = weather["hourly"]["wind_speed_10m"]
    if not times or len(times) != len(winds):
        raise ValueError("Weather time and wind vectors differ")
    stamp = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
    nearest = min(range(len(times)), key=lambda i: abs((times[i] - stamp).total_seconds()))
    separation = abs((times[nearest] - stamp).total_seconds())
    if separation > 1800:
        raise ValueError("No weather hour within thirty minutes of acquisition")
    value = winds[nearest]
    if value is None:
        return None
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid wind value")
    return {
        "candidate": row,
        "wind_speed_10m_m_s": value,
        "weather_time_utc": times[nearest].isoformat(),
        "weather_grid_cell": [weather["longitude"], weather["latitude"]],
        "time_separation_minutes": separation / 60,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve meteorological receipt; use a new output")
    source = json.loads(args.catalogue.read_text())
    rows = source["candidates"]
    start, exclusive_end = source["dates"]
    end = (datetime.fromisoformat(exclusive_end) - timedelta(days=1)).date().isoformat()
    pages = []
    contexts = []
    with httpx.Client(timeout=180) as client:
        for offset in range(0, len(rows), 20):
            batch = rows[offset : offset + 20]
            response = client.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params=weather_parameters(batch, start, end),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and len(batch) == 1:
                payload = [payload]
            if not isinstance(payload, list) or len(payload) != len(batch):
                raise ValueError("Weather result cardinality differs from coordinates")
            pages.append({"request_url": str(response.url), "response": payload})
            for row, weather in zip(batch, payload, strict=True):
                context = wind_context(row, weather)
                if context is not None:
                    contexts.append(context)
            print("Weather context", min(offset + 20, len(rows)), "/", len(rows), flush=True)
    report = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "purpose": "ERA5 acquisition selection context only; not observed wind, SAR truth or unbiased regional evaluation",
        "model": "ERA5 via Open-Meteo",
        "source_catalogue_sha256": file_digest(args.catalogue),
        "driver_sha256": file_digest(Path(__file__)),
        "request_pages": pages,
        "screened": contexts,
        "selection_rule": "Rank southern HH/HV IW metadata by nearest-hour ERA5 10m wind at overlap centroid; predeclare acquisitions before inspecting measurement pixels; retain every rejected acquisition.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    for item in sorted(contexts, key=lambda r: (-r["wind_speed_10m_m_s"], r["candidate"]["name"]))[
        :15
    ]:
        row = item["candidate"]
        print(
            row["name"],
            item["wind_speed_10m_m_s"],
            "m/s",
            row["overlap_centroid"],
            round(row["southern_fraction"], 3),
            flush=True,
        )


if __name__ == "__main__":
    main()
