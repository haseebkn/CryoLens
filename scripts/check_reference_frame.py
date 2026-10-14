"""Reproduce a development reference frame without changing packets or human responses."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from cryolens.data.ai4arctic import load_scene
from cryolens.detect.filters import SuppressionConfig
from cryolens.eval.cohort import SceneRecord, digest, file_digest, load_manifest, verify_sources
from cryolens.reference.audit import candidate_frame
from cryolens.reference.packet import read_workspace, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--manifest", type=Path, default=Path("docs/evaluation/v1/manifest.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = read_workspace(args.workspace)
    if workspace["scene"]["partition"] == "test" or workspace["configuration"] is None:
        raise ValueError("Frame reproduction is development-only; no held-out detector access")
    if digest(load_manifest(args.manifest)) != workspace["manifest_sha256"]:
        raise ValueError("Parent frozen manifest changed")
    record = SceneRecord.model_validate(workspace["scene"])
    verify_sources([record], args.data_root)
    config = workspace["configuration"]
    frame, _, detection = candidate_frame(
        load_scene(args.data_root / record.source_path),
        record.source_sha256,
        config["detector"],
        config["pfa"],
        SuppressionConfig(**config["suppression"]),
        workspace["policy"]["cfar_core_pixels"],
    )
    original = json.loads((args.workspace / "private/frame.json").read_text())
    receipt = {
        "schema_version": 1,
        "purpose": "development_frame_reproduction_no_annotations",
        "checked_at": datetime.now(UTC).isoformat(),
        "workspace_id": workspace["workspace_id"],
        "source_sha256": record.source_sha256,
        "recorded_frame_sha256": workspace["files_sha256"]["private/frame.json"],
        "executing_audit_source_sha256": file_digest(Path("src/cryolens/reference/audit.py")),
        "executing_check_source_sha256": file_digest(Path(__file__)),
        "raw_components_checked": len(frame),
        "exact_frame_reproduced": frame == original,
        "exact_detection_summary_reproduced": detection == workspace["detection"],
        "human_responses_modified": False,
        "heldout_access": False,
    }
    write_json(args.output, receipt)
    if not receipt["exact_frame_reproduced"] or not receipt["exact_detection_summary_reproduced"]:
        raise ValueError(
            "Development frame differs; preserve this packet and prepare a new version"
        )
    print(f"Exactly reproduced {len(frame)} development components; no packet/response changes")


if __name__ == "__main__":
    main()
