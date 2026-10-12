"""Predeclare a small southern IW batch using wind context and annotation guards."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cryolens.config.settings import Settings
from cryolens.eval.cohort import file_digest
from cryolens.ingest.cdse import CDSEClient
from cryolens.preprocess.marine_geocoding import MarineGeocoder


def height_value(node: ET.Element) -> float:
    value = node.findtext("value")
    if value is None:
        raise ValueError("Missing declared reference height")
    return float(value)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wind-screen", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--maximum", type=int, default=3)
    args = p.parse_args()
    if args.maximum < 1:
        p.error("maximum must be positive")
    if args.output_root.exists():
        raise FileExistsError("Preserve prior metadata decisions; use a new directory")
    args.output_root.mkdir(parents=True)
    wind = json.loads(args.wind_screen.read_text())
    eligible = [
        r
        for r in wind["screened"]
        if r["candidate"]["southern_fraction"] >= 0.8
        and r["candidate"]["aoi_fraction"] >= 0.95
        and -57 <= r["candidate"]["overlap_centroid"][0] <= -46
        and 44 <= r["candidate"]["overlap_centroid"][1] <= 49
    ]
    eligible.sort(key=lambda r: (-r["wind_speed_10m_m_s"], r["candidate"]["name"]))
    settings_options: dict[str, Any] = {"_env_file": args.env_file}
    client = CDSEClient(cdse_settings=Settings(**settings_options).cdse)
    selected: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    days: set[str] = set()
    with httpx.Client(timeout=120, follow_redirects=True) as http:
        for item in eligible:
            row = item["candidate"]
            if row["date"][:10] in days:
                continue
            base = f"https://download.dataspace.copernicus.eu/odata/v1/Products({row['id']})/Nodes({row['name']})/Nodes(annotation)"
            listing = http.get(base + "/Nodes")
            listing.raise_for_status()
            nodes = listing.json()["result"]
            decision: dict[str, Any] = {
                "candidate": row,
                "wind_context": {k: v for k, v in item.items() if k != "candidate"},
                "node_listing": listing.json(),
                "channels": {},
            }
            reasons = []
            for pol in ("hh", "hv"):
                names = [
                    n["Name"]
                    for n in nodes
                    if f"-{pol}-" in n["Name"] and n["Name"].endswith(".xml")
                ]
                if len(names) != 1:
                    reasons.append("Annotation missing or ambiguous: " + pol)
                    continue
                uri = base + "/Nodes(" + names[0] + ")/$value"
                response = http.get(
                    uri, headers={"Authorization": "Bearer " + client.get_auth_token()}
                )
                response.raise_for_status()
                path = args.output_root / (row["id"] + "-" + pol + ".xml")
                with path.open("xb") as stream:
                    stream.write(response.content)
                xml = ET.fromstring(response.content)
                heights = [
                    height_value(v) for v in xml.findall(".//terrainHeightList/terrainHeight")
                ]
                channel: dict[str, Any] = {
                    "request_url": uri,
                    "annotation": path.name,
                    "sha256": file_digest(path),
                    "reference_heights_m": heights,
                }
                try:
                    channel["geometry"] = MarineGeocoder(path).provenance()
                except (ValueError, FileNotFoundError) as error:
                    reasons.append(pol + ": " + str(error))
                decision["channels"][pol.upper()] = channel
            decision["qualified_metadata"] = not reasons
            decision["reasons"] = reasons
            decisions.append(decision)
            print(
                row["name"],
                "wind",
                item["wind_speed_10m_m_s"],
                "metadata",
                not reasons,
                reasons,
                flush=True,
            )
            with (args.output_root / (row["id"] + "-decision.json")).open(
                "x", encoding="utf-8"
            ) as stream:
                json.dump(decision, stream, indent=2, allow_nan=False)
            if not reasons:
                selected.append(row)
                days.add(row["date"][:10])
            if len(selected) == args.maximum:
                break
    plan = {
        "schema_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "purpose": "Southern IW processing development; adaptive metadata/wind selection, not operational or unbiased evaluation",
        "selection_rule": wind["selection_rule"]
        + f"; angular polygon-overlap proxies >=0.8 southern and >=0.95 AOI; centroid -57..-46 longitude, 44..49 latitude; distinct dates; first {args.maximum} passing unchanged MarineGeocoder metadata guards",
        "aoi_path": "configs/aoi.geojson",
        "frozen_manifest": "docs/evaluation/v1/manifest.json",
        "quality_policy": "safe-quality-v1 (unchanged)",
        "wind_screen_sha256": file_digest(args.wind_screen),
        "metadata_driver_sha256": file_digest(Path(__file__)),
        "maximum_selected_products": args.maximum,
        "selected": selected,
        "metadata_decisions": decisions,
    }
    with (args.output_root / "acquisition-plan.json").open("x", encoding="utf-8") as stream:
        json.dump(plan, stream, indent=2, allow_nan=False)
    print("Selected without inspecting measurement pixels:", len(selected), flush=True)


if __name__ == "__main__":
    main()
