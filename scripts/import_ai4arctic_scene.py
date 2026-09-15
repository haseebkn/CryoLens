"""Import one real public AI4Arctic scene for the local portfolio dashboard.

Usage: uv run python scripts/import_ai4arctic_scene.py --scene <path_prep.nc>
Defaults to charted open water screening. No synthetic fallback or forecast.
"""

import argparse
import hashlib
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyproj
from shapely.geometry import box
from shapely.ops import transform

from cryolens.data.ai4arctic import load_scene
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session_factory
from cryolens.detect.filters import SuppressionConfig
from cryolens.detect.runner import SceneDetectionRunner
from cryolens.geo.aoi import load_aoi


def acquisition_time(product_id: str) -> datetime:
    """Require an actual Sentinel acquisition timestamp, never use today's time."""
    match = re.search(r"_(\d{8}T\d{6})_", product_id)
    if match is None:
        raise ValueError("Source product ID has no Sentinel-1 acquisition timestamp")
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--pfa", type=float, default=1e-6)
    parser.add_argument(
        "--include-sea-ice",
        action="store_true",
        help="Research mode: retain ice-affected/unknown context.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    scene = load_scene(args.scene)
    observed_at = acquisition_time(scene.original_id)
    product_id = "AI4ARCTIC_" + scene.scene_id
    factory = get_db_session_factory()
    with factory() as session:
        previous = SceneRepository.get_by_product_id(session, product_id)
        if previous is not None:
            print(f"Already imported: {previous.id}; existing reviews preserved.")
            return
    suppression = SuppressionConfig(exclude_sea_ice=not args.include_sea_ice)
    result = SceneDetectionRunner(detector_kind="gamma", pfa=args.pfa, suppression=suppression).run(
        scene
    )
    if result.analysed_area_km2 <= 0:
        raise ValueError("No eligible analyzed area; scene cannot be presented as surveyed")
    with args.scene.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    footprint = box(
        float(np.nanmin(scene.longitude)),
        float(np.nanmin(scene.latitude)),
        float(np.nanmax(scene.longitude)),
        float(np.nanmax(scene.latitude)),
    )
    projector = pyproj.Transformer.from_crs(4326, 3978, always_xy=True)
    provenance = {
        "source": "AI4Arctic ready-to-train",
        "source_product_id": scene.original_id,
        "source_doi": "10.11583/DTU.21316608",
        "source_filename": args.scene.name,
        "source_sha256": checksum,
        "observed_at": observed_at.isoformat(),
        "imported_at": datetime.now(UTC).isoformat(),
        "historical": True,
        "aoi": load_aoi().__geo_interface__,
        "aoi_kind": "research_study_polygon",
        "preprocessing": "publisher_ready_to_train",
        "suppression_config": asdict(suppression),
        "evaluation": result.to_dict(),
        "ais_status": "not_checked",
        "classification_status": "unverified",
        "cog_available": False,
        "footprint_kind": "geolocation_grid_bounding_envelope",
    }
    with factory() as session:
        stored = SceneRepository.create_scene(
            session,
            product_id=product_id,
            platform=scene.original_id.split("_")[0],
            mode="EW",
            polarizations=["HH", "HV"],
            acquisition_time=observed_at,
            cog_path="",
            footprint_wgs84=footprint,
            footprint_epsg3978=transform(projector.transform, footprint),
            processing_provenance=provenance,
            status="DETECTED",
        )
        for target in result.targets:
            DetectionRepository.create_detection(
                session,
                scene_id=stored.id,
                confidence=target.confidence,
                detector_name=result.detector_name,
                predicted_class="unclassified",
                geom_epsg3978=target.geom_epsg3978,
                geom_wgs84=target.geom_wgs84,
                centroid_wgs84=target.centroid_wgs84,
                length_m=target.length_m,
                width_m=target.width_m,
                estimated_area_m2=target.estimated_area_m2,
                peak_sigma0_hv_db=target.peak_sigma0_hv_db,
                mean_sigma0_hv_db=target.mean_sigma0_hv_db,
                peak_sigma0_hh_db=target.peak_sigma0_hh_db,
                hh_hv_ratio_db=target.hh_hv_ratio_db,
                incidence_angle_deg=target.incidence_angle_deg,
                detector_params={"pfa": args.pfa, "pixel_bbox": list(target.pixel_bbox)},
                properties={
                    **target.properties,
                    "ais_status": "not_checked",
                    "source_sha256": checksum,
                    "historical": True,
                },
            )
        session.commit()
        print(
            f"Imported historical scene {stored.id}: {len(result.targets)} unverified candidates, "
            f"{result.analysed_area_km2:.1f} km2 analyzed."
        )


if __name__ == "__main__":
    main()
