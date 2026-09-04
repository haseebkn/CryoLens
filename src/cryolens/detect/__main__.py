"""CLI entrypoint for running CFAR detection and vectorization on processed SAR COGs."""

import argparse
import sys
import time
from datetime import UTC, datetime

import pyproj
import rasterio
import shapely.geometry
import shapely.ops
from rasterio.crs import CRS

from cryolens.config.settings import get_app_config
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session_factory
from cryolens.detect.cfar import get_cfar_detector
from cryolens.geo.vectorize import TargetVectorizer


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for CFAR pipeline."""
    parser = argparse.ArgumentParser(
        description="Run CFAR detection on a preprocessed 4-band Sentinel-1 COG."
    )
    parser.add_argument(
        "--scene",
        type=str,
        required=True,
        help="Scene product ID (e.g., S1B_EW_GRDM_1SDH_20200515T094821_...)",
    )
    parser.add_argument(
        "--pfa",
        type=float,
        default=None,
        help="Target Probability of False Alarm (default: from project.yaml)",
    )
    parser.add_argument(
        "--distribution",
        type=str,
        choices=["cell_averaging", "k_distribution", "gamma"],
        default=None,
        help="CFAR clutter distribution model (default: from project.yaml)",
    )
    parser.add_argument(
        "--save-db",
        action="store_true",
        default=True,
        help="Save scene and detections to PostGIS database.",
    )
    return parser.parse_args()


def main() -> None:
    """Execute end-to-end CFAR detection slice on given scene."""
    args = parse_args()
    config = get_app_config()

    scene_id = args.scene
    processed_dir = config.settings.data_dir / "processed" / scene_id
    cog_path = processed_dir / f"{scene_id}_4band_EPSG3978.tif"

    if not cog_path.is_file():
        # Fallback to search recursively
        found = list(config.settings.data_dir.glob(f"**/{scene_id}*4band*.tif"))
        if found:
            cog_path = found[0]
        else:
            print(
                f"[ERROR] Processed COG not found for scene {scene_id} at {cog_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    print("=== CryoLens CFAR Detector ===")
    print(f"  Scene: {scene_id}")
    print(f"  COG:   {cog_path}")

    start_time = time.perf_counter()

    with rasterio.open(cog_path) as src:
        bounds = src.bounds
        transform = src.transform
        crs = src.crs or CRS.from_epsg(3978)
        width = src.width
        height = src.height

        print(f"  Raster: {width}x{height} px | CRS: {crs} | Bounds: {bounds}")

        # Read bands: Band 1 = HH (dB), Band 2 = HV (dB), Band 4 = Incidence Angle (deg)
        hh_db = src.read(1)
        hv_db = src.read(2)
        inc_deg = src.read(4) if src.count >= 4 else None

    # Instantiate CFAR detector
    detector = get_cfar_detector(
        distribution=args.distribution,
        pfa=args.pfa,
    )
    dist_name = args.distribution or config.project.cfar.distribution
    print(f"  Running {dist_name} CFAR (Pfa={detector.pfa:.1e})...")

    # Run CFAR
    t0 = time.perf_counter()
    cfar_result = detector.detect(
        sigma0_hv_db=hv_db,
        sigma0_hh_db=hh_db,
    )
    cfar_time = time.perf_counter() - t0
    num_hits = int(cfar_result.detection_mask.sum())
    print(f"  CFAR completed in {cfar_time:.2f}s -> {num_hits} raw hit pixels")

    # Vectorize and extract targets
    vectorizer = TargetVectorizer(source_crs="EPSG:3978", target_crs="EPSG:4326")
    targets = vectorizer.extract_targets(
        detection_mask=cfar_result.detection_mask,
        transform=transform,
        sigma0_hv_db=hv_db,
        sigma0_hh_db=hh_db,
        incidence_angle=inc_deg,
        detector_name=dist_name,
    )
    print(f"  Clustered into {len(targets)} targets (min_pixels={vectorizer.min_pixels})")

    # Tally classes
    icebergs = sum(1 for t in targets if t.predicted_class == "iceberg")
    ships = sum(1 for t in targets if t.predicted_class == "ship")
    clutter = sum(1 for t in targets if t.predicted_class == "clutter")
    print(f"  Classification: {icebergs} Icebergs | {ships} Vessels | {clutter} Clutter")

    # Persist to database
    if args.save_db:
        try:
            session_factory = get_db_session_factory()
            with session_factory() as session:
                # 1. Create or get scene
                scene = SceneRepository.get_by_product_id(session, scene_id)
                if scene is None:
                    # Construct scene footprint polygon in EPSG:3978
                    poly_3978 = shapely.geometry.box(
                        bounds.left, bounds.bottom, bounds.right, bounds.top
                    )
                    transformer = pyproj.Transformer.from_crs(
                        "EPSG:3978", "EPSG:4326", always_xy=True
                    )
                    poly_4326 = shapely.ops.transform(transformer.transform, poly_3978)

                    scene = SceneRepository.create_scene(
                        session=session,
                        product_id=scene_id,
                        platform="Sentinel-1B" if "S1B" in scene_id else "Sentinel-1A",
                        mode="EW",
                        polarizations=["HH", "HV"],
                        acquisition_time=datetime.now(UTC),
                        cog_path=str(cog_path),
                        footprint_epsg3978=poly_3978,
                        footprint_wgs84=poly_4326,
                        processing_provenance={
                            "detector": dist_name,
                            "pfa": detector.pfa,
                            "guard_window": [detector.guard_h, detector.guard_w],
                            "bg_window": [detector.bg_h, detector.bg_w],
                        },
                        status="DETECTED",
                    )

                # 2. Insert detections
                for t in targets:
                    DetectionRepository.create_detection(
                        session=session,
                        scene_id=scene.id,
                        confidence=t.confidence,
                        detector_name=dist_name,
                        predicted_class=t.predicted_class,
                        geom_epsg3978=t.geom_epsg3978,
                        geom_wgs84=t.geom_wgs84,
                        centroid_wgs84=t.centroid_wgs84,
                        length_m=t.length_m,
                        width_m=t.width_m,
                        estimated_area_m2=t.estimated_area_m2,
                        peak_sigma0_hv_db=t.peak_sigma0_hv_db,
                        mean_sigma0_hv_db=t.mean_sigma0_hv_db,
                        peak_sigma0_hh_db=t.peak_sigma0_hh_db,
                        hh_hv_ratio_db=t.hh_hv_ratio_db,
                        incidence_angle_deg=t.incidence_angle_deg,
                        detector_params={
                            "pfa": detector.pfa,
                            "pixel_bbox": list(t.pixel_bbox),
                        },
                        properties=t.properties,
                    )
                session.commit()
                print(f"  [SUCCESS] Persisted {len(targets)} detections to PostGIS database.")
        except Exception as exc:
            print(f"  [WARNING] Database save skipped / error: {exc}", file=sys.stderr)

    total_time = time.perf_counter() - start_time
    print(f"=== Total Pipeline Execution Time: {total_time:.2f}s ===")


if __name__ == "__main__":
    main()
