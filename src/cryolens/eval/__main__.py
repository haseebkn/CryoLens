"""Command line entry point for the detection benchmark.

Example:
    python -m cryolens.eval --data-root data/raw/ai4arctic --limit 12 --sweep
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from cryolens.config.settings import get_project_config
from cryolens.data.ai4arctic import build_scene_index, scenes_intersecting_aoi
from cryolens.detect.filters import SuppressionConfig
from cryolens.eval.benchmark import DetectionBenchmark

logger = logging.getLogger(__name__)

DEFAULT_PFA_SWEEP = (1e-4, 1e-5, 1e-6, 1e-7)


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser."""
    p = argparse.ArgumentParser(
        prog="python -m cryolens.eval",
        description="Benchmark CFAR detection density over the Newfoundland & Labrador shelf.",
    )
    p.add_argument("--data-root", default="data/raw/ai4arctic", help="Root of the scene archive.")
    p.add_argument(
        "--index-path",
        default="data/processed/ai4arctic_scene_index.json",
        help="Where to cache the scene extent index.",
    )
    p.add_argument("--output-dir", default="data/processed/benchmarks")
    p.add_argument("--detector", default="gamma", choices=["gamma", "ca"])
    p.add_argument("--pfa", type=float, default=1e-5)
    p.add_argument("--limit", type=int, default=None, help="Cap the number of scenes.")
    p.add_argument("--sweep", action="store_true", help="Also run the Pfa operating-point sweep.")
    p.add_argument(
        "--any-overlap",
        action="store_true",
        help="Accept scenes that merely clip the AOI instead of requiring the centre inside it.",
    )
    p.add_argument("--exclude-sea-ice", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and write artefacts. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = get_project_config()
    bbox = (
        cfg.spatial.bbox.west,
        cfg.spatial.bbox.south,
        cfg.spatial.bbox.east,
        cfg.spatial.bbox.north,
    )
    logger.info("AOI (W,S,E,N) = %s", bbox)

    extents = build_scene_index(args.data_root, args.index_path)
    if not extents:
        logger.error("No scenes found under %s", args.data_root)
        return 1

    selected = scenes_intersecting_aoi(extents, bbox, require_centre=not args.any_overlap)
    logger.info("%d of %d scenes fall in the AOI", len(selected), len(extents))
    if not selected:
        logger.error("No scenes inside the AOI; nothing to benchmark.")
        return 1

    suppression = SuppressionConfig(exclude_sea_ice=args.exclude_sea_ice)
    bench = DetectionBenchmark(output_dir=args.output_dir, suppression=suppression)

    results = bench.run_scene_set(selected, args.detector, args.pfa, args.limit)
    if not results:
        logger.error("Every scene failed to load; nothing to report.")
        return 1

    sweep = None
    if args.sweep:
        sweep = bench.sweep_pfa(selected, DEFAULT_PFA_SWEEP, args.detector, args.limit)

    label = "Gamma-CFAR" if args.detector == "gamma" else "CA-CFAR"
    report = bench.write_report(results, sweep, detector_label=label)

    overall = report["overall"]
    print()
    print(f"Scenes analysed          : {overall['n_scenes']}")
    print(f"Water area analysed      : {overall['area_km2']:,.0f} km2")
    print(f"Raw CFAR per 1000 km2    : {overall['raw_density_per_1000km2']:.2f}")
    print(f"Final per 1000 km2       : {overall['density_per_1000km2']:.2f}")
    print(f"Suppression factor       : {overall['suppression_factor']:.1f}x")
    print(f"Artefacts written to     : {Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
