"""Preprocess an existing Sentinel-1 SAFE product; no synthetic fallbacks."""

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from cryolens.config.settings import get_app_config
from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.quality import (
    ProcessingQualityError,
    assess_safe_product,
    record_processing_failure,
    require_channels,
)
from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.snap_chain import SNAPChainRunner
from cryolens.preprocess.stack import COGStackBuilder

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preprocess real Sentinel-1 HH/HV SAFE data")
    parser.add_argument("--scene", required=True, help="Path to an existing .SAFE product")
    parser.add_argument("--engine", choices=["python", "snap"], default=None)
    parser.add_argument("--output-dir", default="./data/processed")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO)
    config = get_app_config()
    safe = Path(args.scene)
    quality_path = Path(args.output_dir) / safe.stem / "processing-quality.json"
    if not safe.is_dir():
        logger.error("Input SAFE product does not exist: %s", safe)
        return 1
    try:
        if (args.engine or config.project.preprocessing.engine) == "snap":
            SNAPChainRunner().run_preprocessing(safe, output_dir=args.output_dir)
            return 0
        reader = SAFEProductReader(safe)
        assess_safe_product(reader, Path(args.output_dir) / safe.stem / "processing-quality.json")
        hh = reader.read_sigma0("HH")
        hv = reader.read_sigma0("HV")
        require_channels(hh, hv, Path(args.output_dir) / safe.stem / "processing-quality.json")
        processor = PurePythonSARProcessor(
            target_crs=config.project.spatial.target_crs,
            pixel_spacing_m=config.project.spatial.pixel_spacing_m,
        )
        result = processor.process_calibrated_arrays(
            hh["sigma0_linear"],
            hv["sigma0_linear"],
            hh["incidence_angle_deg"],
            hh["latitude"],
            hh["longitude"],
            apply_denoise=False,
        )
        output = COGStackBuilder(args.output_dir).build_and_export_cog(
            scene_id=safe.stem,
            bands=result["bands"],
            transform=result["transform"],
            crs=result["crs"],
            nodata=result["nodata"],
            provenance={"source_kind": "sentinel1_safe", "processing_quality": "passed"},
        )
        logger.info(
            "Wrote %s using annotation GCPs; precise orbit and terrain correction are not applied.",
            output,
        )
        return 0
    except (ValueError, OSError, RuntimeError, ET.ParseError) as exc:
        if not isinstance(exc, ProcessingQualityError):
            record_processing_failure(quality_path, safe.name, "preprocessing", exc)
        logger.error("Preprocessing failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
