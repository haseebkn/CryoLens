"""End-to-end pipeline orchestrator: CDSE search -> calibrate -> detect -> PostGIS."""

import logging
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyproj
import rasterio
import shapely.geometry
import shapely.ops
from rasterio.crs import CRS

from cryolens.config.settings import get_app_config
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session_factory
from cryolens.detect.cfar import get_cfar_detector
from cryolens.detect.filters import (
    SuppressionConfig,
    build_analysis_mask,
    deduplicate_across_tiles,
    filter_targets,
)
from cryolens.geo.vectorize import TargetVectorizer
from cryolens.ingest.cdse import CDSEClient, SARSceneMetadata
from cryolens.preprocess.masks import LandMaskGenerator
from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.snap_chain import SNAPChainRunner
from cryolens.preprocess.stack import COGStackBuilder

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Orchestrates end-to-end processing of Sentinel-1 scenes."""

    def __init__(self):
        self.config = get_app_config()
        self.cdse_client = CDSEClient()
        self.session_factory = get_db_session_factory()
        self.data_dir = self.config.settings.data_dir
        self.output_dir = self.data_dir / "processed"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_batch(
        self,
        start_date: datetime,
        end_date: datetime,
        bbox: list[float] | None = None,
        limit: int = 5,
    ) -> int:
        """Run the pipeline on a batch of scenes from CDSE."""
        if not bbox:
            c_bbox = self.config.project.spatial.bbox
            bbox = [c_bbox.west, c_bbox.south, c_bbox.east, c_bbox.north]

        logger.info(f"Searching for scenes between {start_date} and {end_date} in bbox {bbox}")
        scenes = self.cdse_client.search_scenes(
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

        logger.info(f"Found {len(scenes)} scenes to process.")
        processed_count = 0

        for scene in scenes:
            try:
                self.process_scene(scene)
                processed_count += 1
            except Exception as e:
                logger.error(f"Failed to process scene {scene.scene_id}: {e}", exc_info=True)

        return processed_count

    def process_scene(self, scene: SARSceneMetadata) -> None:
        """Process a single scene end-to-end."""
        logger.info(f"--- Processing Scene: {scene.scene_id} ---")

        # 1. Download
        safe_dir = self.cdse_client.download_scene(scene, output_dir=self.data_dir / "raw")

        # 2. Preprocess -> COG
        engine = self.config.project.preprocessing.engine
        cog_path = self._run_preprocessing(scene, safe_dir, engine)

        # 3. Detect
        self._run_detection(scene, cog_path)

    def _run_preprocessing(self, scene: SARSceneMetadata, safe_dir: Path, engine: str) -> Path:
        """Calibrate a downloaded SAFE product and return the 4-band COG path.

        Reads the real measurement rasters and calibration annotations; there is
        no synthetic fallback. If the product cannot be calibrated the scene
        fails loudly, because a detection produced from fabricated backscatter is
        worse than no detection at all.
        """
        logger.info("Preprocessing %s with engine '%s'...", safe_dir.name, engine)

        if engine == "snap":
            runner = SNAPChainRunner()
            runner.run_preprocessing(safe_dir)
            logger.info("SNAP graph complete; reading calibrated output via the SAFE reader.")

        reader = SAFEProductReader(safe_dir)
        available = reader.available_polarisations()
        if "HH" not in available or "HV" not in available:
            raise ValueError(
                f"{safe_dir.name} carries polarisations {available}; CryoLens requires "
                "dual-pol HH+HV (ADR-002). Filter the catalogue query on polarisation."
            )

        hh = reader.read_sigma0("HH", remove_thermal_noise=True)
        hv = reader.read_sigma0("HV", remove_thermal_noise=True)

        processor = PurePythonSARProcessor(
            target_crs=self.config.project.spatial.target_crs,
            pixel_spacing_m=self.config.project.spatial.pixel_spacing_m,
        )
        result = processor.process_calibrated_arrays(
            sigma0_hh_linear=hh["sigma0_linear"],
            sigma0_hv_linear=hv["sigma0_linear"],
            incidence_angle_deg=hh["incidence_angle_deg"],
            latitude=hh["latitude"],
            longitude=hh["longitude"],
            apply_denoise=self.config.project.preprocessing.s1denoise.enabled,
        )

        stack_builder = COGStackBuilder(output_dir=self.output_dir / scene.scene_id)
        cog_path = stack_builder.build_and_export_cog(
            scene_id=scene.scene_id,
            bands=result["bands"],
            transform=result["transform"],
            crs=result["crs"],
        )
        logger.info("Wrote calibrated 4-band COG: %s", cog_path)
        return cog_path

    def _run_detection(self, scene: SARSceneMetadata, cog_path: Path) -> None:
        """Run CFAR detection on the preprocessed COG and save to DB."""
        logger.info(f"Running Detection on {cog_path}...")

        with rasterio.open(cog_path) as src:
            bounds = src.bounds
            transform = src.transform
            crs = src.crs or CRS.from_epsg(3978)
            hh_db = src.read(1)
            hv_db = src.read(2)
            inc_deg = src.read(4) if src.count >= 4 else None

        detector = get_cfar_detector(
            distribution=self.config.project.cfar.distribution,
            pfa=self.config.project.cfar.default_pfa,
        )

        # Land, coastal buffer, swath borders and subswath seams are excluded
        # before detection so that they cannot contaminate the clutter estimate.
        suppression = SuppressionConfig()
        valid = np.isfinite(hv_db) & (hv_db > -90.0)
        land_mask = LandMaskGenerator().generate_land_mask(hv_db.shape, transform, str(crs))
        # Convert the boolean land mask into the ordinal zone convention the
        # suppression chain expects: 0 is land, a large value is open ocean.
        land_zone = np.where(land_mask, 0, 99).astype(np.int16)
        analysis_mask, mask_breakdown = build_analysis_mask(
            valid_mask=valid,
            land_distance_zone=land_zone,
            sic_class=None,
            sigma0_hv_db=hv_db,
            config=suppression,
        )

        t0 = time.perf_counter()
        cfar_result = detector.detect(
            sigma0_hv_db=hv_db, valid_mask=analysis_mask, sigma0_hh_db=hh_db
        )
        logger.info(
            "CFAR completed in %.2fs -> %d raw hits",
            time.perf_counter() - t0,
            int(cfar_result.detection_mask.sum()),
        )

        vectorizer = TargetVectorizer(source_crs="EPSG:3978", target_crs="EPSG:4326")
        candidates = vectorizer.extract_targets(
            detection_mask=cfar_result.detection_mask,
            transform=transform,
            sigma0_hv_db=hv_db,
            sigma0_hh_db=hh_db,
            incidence_angle=inc_deg,
            detector_name=detector.__class__.__name__,
        )

        targets, suppression_stats = filter_targets(
            candidates, config=suppression, clutter_mean_db=cfar_result.clutter_mean_db
        )
        targets = deduplicate_across_tiles(targets)

        logger.info(
            "Extracted %d targets from %d candidates.\n%s",
            len(targets),
            len(candidates),
            suppression_stats.format_table(),
        )

        with self.session_factory() as session:
            # Upsert Scene
            db_scene = SceneRepository.get_by_product_id(session, scene.scene_id)
            if not db_scene:
                poly_3978 = shapely.geometry.box(bounds.left, bounds.bottom, bounds.right, bounds.top)
                transformer = pyproj.Transformer.from_crs("EPSG:3978", "EPSG:4326", always_xy=True)
                poly_4326 = shapely.ops.transform(transformer.transform, poly_3978)

                db_scene = SceneRepository.create_scene(
                    session=session,
                    product_id=scene.scene_id,
                    platform=scene.platform,
                    mode=scene.instrument_mode,
                    polarizations=scene.polarizations,
                    acquisition_time=scene.acquisition_time,
                    cog_path=str(cog_path),
                    footprint_epsg3978=poly_3978,
                    footprint_wgs84=poly_4326,
                    processing_provenance={
                        "detector": detector.__class__.__name__,
                        "pfa": detector.pfa,
                    },
                    status="DETECTED",
                )

            # Insert Detections
            for t in targets:
                DetectionRepository.create_detection(
                    session=session,
                    scene_id=db_scene.id,
                    confidence=t.confidence,
                    detector_name=detector.__class__.__name__,
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
                    detector_params={"pfa": detector.pfa, "pixel_bbox": list(t.pixel_bbox)},
                    properties=t.properties,
                )

            session.commit()
            logger.info(f"Saved scene and {len(targets)} detections to database.")
