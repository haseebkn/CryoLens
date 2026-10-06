"""Search evaluated SAR acquisitions, generate native pairs and report availability."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as transform_geometry
from sqlalchemy import select

from cryolens.config.settings import get_settings
from cryolens.db.models import DetectionModel, OpticalPairModel
from cryolens.db.session import get_db_session_factory
from cryolens.eval.cohort import digest, file_digest, load_manifest
from cryolens.geo.aoi import contains_point
from cryolens.ingest.sentinel2 import Sentinel2Client, Sentinel2Provider, utc
from cryolens.ingest.sentinel2_cdse import CDSESentinel2Client
from cryolens.review.pairs import build_pair, radar_chip
from cryolens.review.policy import PairingPolicy


def catalogue_evaluated(
    client: Sentinel2Provider, output: Path, policy: PairingPolicy
) -> dict[str, Any]:
    benchmark = json.loads(Path("docs/benchmarks/audited_results.json").read_text())
    cohort = load_manifest(Path("docs/evaluation/v1/manifest.json"))
    names = {s["scene_id"] for s in benchmark["scenes"]}
    records = [r for r in cohort["scenes"] if r["scene_id"] in names and r["partition"] != "test"]
    if len(records) != len(names):
        raise ValueError("Every evaluated scene must have an eligible frozen development record")
    report: dict[str, Any] = {
        "schema_version": 1,
        "provider": client.provider,
        "policy": policy.model_dump(),
        "denominator": "evaluated development SAR acquisitions",
        "benchmark_sha256": file_digest(Path("docs/benchmarks/audited_results.json")),
        "manifest_sha256": digest(cohort),
        "scenes": [],
    }
    for record in records:
        try:
            result = client.search(
                record["footprint"], utc(record["acquired_utc"]), policy.search_hours
            )
            entry = {
                "scene_id": record["scene_id"],
                "sar_acquired_utc": record["acquired_utc"],
                "search": result,
                "overlapping_optical_items": len(result["items"]),
                "query_succeeded": True,
            }
        except Exception as exc:
            entry = {
                "scene_id": record["scene_id"],
                "query_succeeded": False,
                "reason": type(exc).__name__ + ": catalogue unavailable",
            }
        report["scenes"].append(entry)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            record["scene_id"], entry.get("overlapping_optical_items", "query failed"), flush=True
        )
    report["total_scenes"] = len(records)
    report["scenes_with_catalogue_overlap"] = sum(
        bool(s.get("overlapping_optical_items")) for s in report["scenes"]
    )
    report["query_failures"] = sum(not s["query_succeeded"] for s in report["scenes"])
    report["interpretation"] = (
        "Footprint overlap only, not candidate visibility, valid pixels or target corroboration"
    )
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue-evaluated", action="store_true")
    parser.add_argument("--scene-id")
    parser.add_argument("--candidate-id", help="Inspect one stored development candidate")
    parser.add_argument(
        "--provider", choices=["planetary-computer", "cdse"], default="planetary-computer"
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--policy", type=Path, default=Path("configs/optical-review-v1.json"))
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 1000:
        raise ValueError("Candidate limit must be between 1 and 1000")
    settings = get_settings()
    root = settings.data_dir / "processed/optical-review"
    root.mkdir(parents=True, exist_ok=True)
    policy = PairingPolicy.model_validate_json(args.policy.read_text())
    client: Sentinel2Provider = (
        CDSESentinel2Client() if args.provider == "cdse" else Sentinel2Client()
    )
    catalogue_filename = (
        "catalogue-coverage.json"
        if args.provider == "planetary-computer"
        else "catalogue-coverage-cdse.json"
    )
    candidate_filename = (
        "candidate-coverage.json"
        if args.provider == "planetary-computer"
        else "candidate-coverage-cdse.json"
    )
    if args.catalogue_evaluated:
        report = catalogue_evaluated(client, root / catalogue_filename, policy)
        return 1 if report["query_failures"] else 0
    with get_db_session_factory()() as session:
        query = select(DetectionModel).order_by(DetectionModel.scene_id, DetectionModel.id)
        if args.scene_id:
            query = query.where(DetectionModel.scene_id == args.scene_id)
        if args.candidate_id:
            query = query.where(DetectionModel.id == args.candidate_id)
        candidates = list(session.scalars(query.limit(args.limit + 1)))
        if args.candidate_id and not candidates:
            raise ValueError("Requested candidate was not found")
        truncated = len(candidates) > args.limit
        candidates = candidates[: args.limit]
        report = {
            "schema_version": 1,
            "provider": client.provider,
            "started_at": datetime.now(UTC).isoformat(),
            "policy": policy.model_dump(),
            "selection": "stored candidate IDs ordered by scene/id; bounded pilot, not a regional sample",
            "candidate_limit": args.limit,
            "selection_truncated": truncated,
            "candidates": [],
        }
        searches = {}
        for candidate in candidates:
            point = (
                to_shape(candidate.centroid_wgs84) if candidate.centroid_wgs84 is not None else None
            )
            if point is None or not contains_point(point.x, point.y):
                continue
            metadata = candidate.scene.processing_provenance
            # Refuse sealed/unknown inputs before requesting optical context.
            radar = radar_chip(candidate, settings.data_dir / "raw", policy.maximum_review_radius_m)
            scene_id = candidate.scene_id
            if scene_id not in searches:
                try:
                    searches[scene_id] = client.search(
                        mapping(to_shape(candidate.scene.footprint_wgs84)),
                        utc(candidate.scene.acquisition_time),
                        policy.search_hours,
                    )
                except Exception as exc:
                    searches[scene_id] = {
                        "items": [],
                        "complete": False,
                        "reason": type(exc).__name__ + ": catalogue unavailable",
                    }
            search = searches[scene_id]
            projector = Transformer.from_crs(4326, 3978, always_xy=True)
            x, y = projector.transform(point.x, point.y)
            view = box(
                x - policy.maximum_review_radius_m,
                y - policy.maximum_review_radius_m,
                x + policy.maximum_review_radius_m,
                y + policy.maximum_review_radius_m,
            )
            relevant = [
                item
                for item in search["items"]
                if transform_geometry(projector.transform, shape(item["geometry"])).intersects(view)
            ]
            relevant.sort(
                key=lambda item: (
                    not shape(item["geometry"]).covers(point),
                    abs(
                        (
                            utc(item["properties"]["datetime"])
                            - utc(candidate.scene.acquisition_time)
                        ).total_seconds()
                    ),
                    item["id"],
                )
            )
            selected = relevant[: policy.max_items_per_candidate]
            pairs = []
            failures = []
            for item in selected:
                try:
                    pairs.append(build_pair(candidate, radar, item, client, root, policy, search))
                except Exception as exc:
                    failures.append(
                        {
                            "item_id": item["id"],
                            "reason": type(exc).__name__
                            + ": optical read/processing unavailable; signed URLs are not retained",
                        }
                    )
            if not pairs:
                reason = (
                    "Optical asset acquisition failed"
                    if failures
                    else "Catalogue search unavailable"
                    if not search["complete"]
                    else "No optical tile covering the candidate within the temporal window"
                )
                pairs.append(
                    build_pair(candidate, radar, None, client, root, policy, search, reason)
                )
            for pair in pairs:
                checksum = file_digest(root / pair["id"] / "pair.json")
                existing = session.get(OpticalPairModel, pair["id"])
                if existing is not None and existing.manifest_sha256 != checksum:
                    raise ValueError("Registered optical pair manifest changed")
                if existing is None:
                    session.add(
                        OpticalPairModel(
                            id=pair["id"],
                            detection_id=candidate.id,
                            manifest_sha256=checksum,
                            metadata_json=pair,
                        )
                    )
            session.commit()
            entry = {
                "detection_id": candidate.id,
                "sar_scene_id": scene_id,
                "sar_source_filename": metadata["source_filename"],
                "candidate_lonlat": [point.x, point.y],
                "catalogue_complete": search["complete"],
                "candidate_overlapping_items": len(relevant),
                "item_selection_truncated": len(relevant) > len(selected),
                "asset_failures": failures,
                "pair_ids": [p["id"] for p in pairs],
                "optical_downloaded": any(p["optical_item_id"] for p in pairs),
                "screened_useful_optical": any(p["visibility"]["useful_for_review"] for p in pairs),
            }
            report["candidates"].append(entry)
            (root / candidate_filename).write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                candidate.id,
                entry["optical_downloaded"],
                entry["screened_useful_optical"],
                flush=True,
            )
        count = len(report["candidates"])
        report.update(
            total_candidates=count,
            candidates_with_downloaded_optical=sum(
                c["optical_downloaded"] for c in report["candidates"]
            ),
            candidates_with_screened_useful_optical=sum(
                c["screened_useful_optical"] for c in report["candidates"]
            ),
            finished_at=datetime.now(UTC).isoformat(),
            analyst_corroboration_measured=False,
            missing_optical_is_false_target=False,
        )
        (root / candidate_filename).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
