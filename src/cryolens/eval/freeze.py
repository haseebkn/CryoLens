"""Create, verify and log a versioned evaluation freeze without running models.

Usage: python -m cryolens.eval.freeze --help
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from cryolens.eval.cohort import (
    SceneRecord,
    assign_partitions,
    digest,
    inventory_ai4arctic,
    inventory_safe,
    load_manifest,
    read_aoi,
    verify_sources,
    write_frozen_bundle,
)
from cryolens.eval.tracking import code_provenance, tracked_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify", "log"])
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--protocol", type=Path, default=Path("configs/evaluation/protocol-v1.json")
    )
    parser.add_argument(
        "--exposure", type=Path, default=Path("configs/evaluation/exposure-v1.json")
    )
    parser.add_argument(
        "--new-source", type=Path, default=Path("configs/evaluation/new-source-v1.json")
    )
    parser.add_argument(
        "--safe-status", type=Path, default=Path("data/processed/downloads-20260918/status.json")
    )
    parser.add_argument("--output", type=Path, default=Path("docs/evaluation/v1"))
    parser.add_argument("--tracking-uri", default="sqlite:///data/processed/mlflow.db")
    args = parser.parse_args()
    if args.action == "create":
        if args.output.exists():
            raise FileExistsError("Frozen output exists; choose a new version/reproduction folder")
        protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
        exposure = json.loads(args.exposure.read_text(encoding="utf-8"))
        if args.new_source.exists():
            new_source = json.loads(args.new_source.read_text(encoding="utf-8"))
            exposure["acquisitions"][new_source["acquisition_id"]] = {
                "exposure": new_source["exposure"],
                "reason": new_source["reason"],
            }
        else:
            new_source = None
        aoi = read_aoi(args.project_root / "configs/aoi.geojson", protocol["aoi_feature_id"])
        reserved_source = args.data_root / new_source["source_path"] if new_source else None
        records = inventory_ai4arctic(args.data_root, aoi, exposure, reserved_source)
        records += inventory_safe(args.data_root, args.safe_status, aoi, exposure)
        if new_source is not None:
            source = next((r for r in records if r.scene_id == new_source["scene_id"]), None)
            if source is None or source.source_sha256 != new_source["source_sha256"]:
                raise ValueError(
                    "New held-out source is missing, outside the AOI, or differs from publisher-verified receipt"
                )
        if not records:
            raise ValueError("No real acquisitions intersect the AOI")
        records = assign_partitions(records, protocol)
        payload = {
            "schema_version": 1,
            "protocol": protocol,
            "protocol_sha256": digest(protocol),
            "aoi": aoi,
            "aoi_sha256": digest(aoi),
            "exposure_policy": exposure,
            "new_source_provenance": new_source,
            "scenes": [r.model_dump(mode="json") for r in records],
            "scope_provenance": code_provenance(args.project_root),
            "limitations": [
                "Existing AI4Arctic data are development-only, including publisher test files previously inspected.",
                "SAFE test scenes need validated processing and independent target/identity annotations before scoring.",
                "New publisher-labelled segmentation test input is metadata-only; valid chart coverage must be established at locked evaluation time. One group is only a pilot.",
                "Metadata/integrity-only exposure is based on the recorded project history, not proof that no human has viewed the files.",
            ],
        }
        write_frozen_bundle(args.output, payload)
    manifest_path = args.output / "manifest.json"
    manifest = load_manifest(manifest_path)
    records = [SceneRecord.model_validate(r) for r in manifest["scenes"]]
    if args.action == "verify":
        verify_sources(records, args.data_root)
    summary = {
        "manifest_sha256": digest(manifest),
        "records": len(records),
        "partitions": dict(Counter(r.partition for r in records)),
        "groups": len({r.group_id for r in records}),
        "test_scene_ids": [r.scene_id for r in records if r.partition == "test"],
    }
    if args.action == "log":
        with tracked_experiment(
            manifest_path,
            args.project_root,
            args.data_root,
            "target_detection",
            "scope",
            {"purpose": "freeze_scope_only", "model_evaluated": False},
            args.tracking_uri,
        ) as run:
            run.log_artifact(args.output / "coverage.geojson", "scope")
            run.log_artifact(args.output / "coverage.png", "scope")
            summary["mlflow_run_id"] = run.run_id
        receipt = args.project_root / "data/processed/evaluation-freeze-tracking.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
