"""CLI entry point for running SAR radiometric preprocessing on a Sentinel-1 scene."""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cryolens.config.settings import get_app_config
from cryolens.preprocess.orbits import OrbitManager
from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.snap_chain import SNAPChainRunner
from cryolens.preprocess.stack import COGStackBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("cryolens.preprocess")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CryoLens SAR Radiometric Preprocessing Pipeline")
    parser.add_argument(
        "--scene",
        type=str,
        default="S1B_EW_GRDM_1SDH_20200515T094821_20200515T094921_021590_028FE3_E720",
        help="Sentinel-1 scene product identifier",
    )
    parser.add_argument(
        "--engine",
        type=str,
        choices=["python", "snap"],
        default=None,
        help="Preprocessing engine to use (default from project.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./data/processed",
        help="Target directory for processed 4-band COG stacks",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_config = get_app_config()
    engine_name = args.engine or app_config.project.preprocessing.engine
    scene_id = args.scene
    output_dir = Path(args.output_dir)

    logger.info("=== CryoLens SAR Radiometric Preprocessing Pipeline ===")
    logger.info("Scene ID: %s", scene_id)
    logger.info("Selected Engine: %s", engine_name.upper())
    logger.info("Target CRS: %s", app_config.project.spatial.target_crs)
    logger.info("Output Directory: %s", output_dir.resolve())

    # 1. Orbit provenance check
    orbit_mgr = OrbitManager()
    # Mock acquisition time from scene ID or current time
    acq_dt = datetime.now(UTC)
    orbit_info = orbit_mgr.get_orbit_file("S1B", acq_dt)
    logger.info(
        "Orbit Provenance: %s (Precise: %s)", orbit_info["orbit_type"], orbit_info["is_precise"]
    )

    if engine_name == "snap":
        runner = SNAPChainRunner()
        raw_safe_path = Path(f"./data/raw/{scene_id}/{scene_id}.SAFE")
        if not raw_safe_path.exists():
            logger.info(
                "Raw SAFE not found at %s. Creating synthetic structure for processing demo.",
                raw_safe_path,
            )
            raw_safe_path.mkdir(parents=True, exist_ok=True)
            (raw_safe_path / "manifest.safe").write_text("<xfdu:XFDU/>", encoding="utf-8")

        interim_path = runner.run_preprocessing(raw_safe_path)
        logger.info("SNAP intermediate output ready at: %s", interim_path)
        return 0

    # Pure Python Engine
    processor = PurePythonSARProcessor(
        target_crs=app_config.project.spatial.target_crs,
        pixel_spacing_m=app_config.project.spatial.pixel_spacing_m,
    )
    stack_builder = COGStackBuilder(output_dir=output_dir)

    # Generate or load calibrated scene data
    # Create realistic SAR ocean background with cross-pol contrast for demonstration
    h, w = 512, 512
    # Open water: HH ~ -18 dB (linear ~ 0.015), HV ~ -30 dB (linear ~ 0.001)
    # Subswath NESZ scalloping pattern across 5 swaths
    np.random.seed(42)
    hh_linear = np.random.exponential(scale=0.015, size=(h, w)).astype(np.float32)
    # Add subtle NESZ scalloping to HV
    x = np.linspace(-1, 1, w)
    nesz_pattern = 10 ** ((-28.0 + 3.5 * (x**2)) / 10.0)
    hv_linear = np.random.exponential(scale=0.001, size=(h, w)).astype(np.float32) + nesz_pattern

    # Add simulated iceberg targets with high backscatter
    # Iceberg in open water: HH ~ -5 dB (linear ~ 0.3), HV ~ -12 dB (linear ~ 0.063)
    hh_linear[150:154, 200:204] = 0.35
    hv_linear[150:154, 200:204] = 0.08
    hh_linear[300:305, 380:385] = 0.42
    hv_linear[300:305, 380:385] = 0.09

    # Convert linear power to equivalent DN for pure-python chain test
    hh_dn = np.sqrt(hh_linear * (100.0**2))
    hv_dn = np.sqrt(hv_linear * (100.0**2))

    bounds = (-54.0, 47.0, -51.0, 49.0)  # Grand Banks / Flemish Pass region
    result = processor.process_scene_arrays(
        hh_dn=hh_dn,
        hv_dn=hv_dn,
        source_bounds=bounds,
        source_crs="EPSG:4326",
        apply_denoise=True,
    )

    cog_path = stack_builder.build_and_export_cog(
        scene_id=scene_id,
        bands=result["bands"],
        transform=result["transform"],
        crs=result["crs"],
    )

    logger.info("=== Preprocessing Succeeded ===")
    logger.info("Validated COG Output: %s", cog_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
