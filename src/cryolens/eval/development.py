"""Tracked development benchmark using only a verified frozen train/validation set."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from shapely.geometry import shape

from cryolens.data.ai4arctic import SceneExtent
from cryolens.detect.filters import SuppressionConfig
from cryolens.eval.benchmark import DetectionBenchmark
from cryolens.eval.tracking import tracked_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("docs/evaluation/v1/manifest.json"))
    parser.add_argument("--partition", choices=["train", "validation"], default="train")
    parser.add_argument("--data-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--tracking-uri", default="sqlite:///data/processed/mlflow.db")
    args = parser.parse_args()
    configuration = json.loads(args.configuration.read_text(encoding="utf-8"))
    if set(configuration) != {"detector", "pfa", "suppression"}:
        raise ValueError("Configuration must specify only detector, pfa and suppression")
    suppression = SuppressionConfig(**configuration["suppression"])
    configuration["suppression"] = asdict(suppression)
    if args.output.exists():
        raise FileExistsError("Choose a new output directory for each experiment")
    with tracked_experiment(
        args.manifest,
        Path.cwd(),
        args.data_root,
        "target_detection",
        args.partition,
        configuration,
        args.tracking_uri,
    ) as run:
        extents = []
        for record in run.records:
            if record.family != "ai4arctic":
                continue  # Fresh SAFE detection is blocked on preprocessing validation.
            west, south, east, north = shape(record.footprint).bounds
            extents.append(
                SceneExtent(
                    path=args.data_root / record.source_path,
                    scene_id=record.scene_id,
                    original_id=record.acquisition_id,
                    ice_service="recorded_in_source",
                    lat_min=south,
                    lat_max=north,
                    lon_min=west,
                    lon_max=east,
                )
            )
        if not extents:
            raise ValueError(
                "No AI4Arctic development inputs; SAFE processing is not yet validated"
            )
        benchmark = DetectionBenchmark(args.output, suppression)
        results = benchmark.run_scene_set(extents, configuration["detector"], configuration["pfa"])
        benchmark.write_report(results, detector_label=configuration["detector"])
        run.client.set_tag(run.run_id, "consumed_family", "ai4arctic")
        run.client.log_param(run.run_id, "consumed_scenes", len(extents))
        area = sum(r.analysed_area_km2 for r in results)
        count = sum(len(r.targets) for r in results)
        metrics = {
            "processed_scenes": float(len(results)),
            "eligible_area_km2": area,
            "retained_candidates": float(count),
        }
        if area > 0:
            metrics["candidate_density_per_1000km2"] = 1000 * count / area
        run.log_metrics(metrics)
        for path in sorted(args.output.glob("*.json")):
            run.log_artifact(path)
        print(json.dumps({"run_id": run.run_id, "metrics": metrics}, indent=2))


if __name__ == "__main__":
    main()
