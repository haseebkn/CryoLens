"""Preprocess an existing Sentinel-1 SAFE product; no synthetic fallbacks."""

import argparse
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from cryolens.config.settings import get_app_config
from cryolens.preprocess.snap_chain import SNAPChainRunner

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preprocess real Sentinel-1 HH/HV SAFE data")
    parser.add_argument("--scene", required=True, help="Path to an existing .SAFE product")
    parser.add_argument("--engine", choices=["python", "snap"], default=None)
    parser.add_argument("--output-dir", default="./data/processed")
    parser.add_argument("--block-size", type=int, default=256)
    parser.add_argument("--scan-rows", type=int, default=32)
    parser.add_argument(
        "--grid-reference",
        type=Path,
        help="Optional independently generated EPSG:3978/40 m reference grid phase",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO)
    config = get_app_config()
    safe = Path(args.scene)
    if not safe.is_dir():
        logger.error("Input SAFE product does not exist: %s", safe)
        return 1
    try:
        if (args.engine or config.project.preprocessing.engine) == "snap":
            SNAPChainRunner().run_preprocessing(safe, output_dir=args.output_dir)
            return 0
        from cryolens.preprocess.product import process_safe_product

        output = process_safe_product(
            safe, Path(args.output_dir), args.block_size, args.scan_rows, args.grid_reference
        )
        logger.info(
            "Wrote %s using marine range-Doppler geocoding with EGM96 sea-surface height.",
            output,
        )
        return 0
    except (ValueError, OSError, RuntimeError, ET.ParseError) as exc:
        logger.error("Preprocessing failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
