"""Prepare a development audit and a distinct held-out human survey, without model evaluation."""

import argparse
from pathlib import Path

from cryolens.eval.cohort import digest, load_manifest
from cryolens.reference.design import ReferencePolicy
from cryolens.reference.packet import prepare, read_workspace, write_json
from cryolens.reference.report import summarize
from cryolens.reference.tracking import log_reference


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root", type=Path, default=Path("data/processed/reference/pilot-v1")
    )
    parser.add_argument(
        "--reuse-verified",
        action="store_true",
        help="Reuse existing hash-verified packets without reprocessing or rewriting annotations",
    )
    args = parser.parse_args()
    project = Path.cwd()
    manifest_path = Path("docs/evaluation/v1/manifest.json")
    policy_path = Path("configs/reference/protocol-v1.json")
    manifest = load_manifest(manifest_path)
    policy = ReferencePolicy.model_validate_json(policy_path.read_text())
    args.output_root.mkdir(parents=True, exist_ok=True)
    release = {
        "purpose": "independent_reference_only",
        "manifest_sha256": digest(manifest),
        "reference_protocol_sha256": digest(policy.model_dump(mode="json")),
        "scene_ids": ["20190119T101218_cis_prep.nc"],
        "detector_execution": False,
        "chart_label_access": False,
        "authorization": "User requested Step 4 independent held-out reference preparation and chose to personally review the pilot. This release authorizes SAR reference preparation only; model evaluation and chart labels stay sealed.",
    }
    release_path = args.output_root / "reference-access-release.json"
    if not release_path.exists():
        write_json(release_path, release)
    for name, scene_id, survey_only in (
        ("development", "20180428T093937_cis_prep.nc", False),
        ("heldout", "20190119T101218_cis_prep.nc", True),
    ):
        root = args.output_root / name
        if root.exists() and args.reuse_verified:
            payload = read_workspace(root)
            if (
                payload["policy_sha256"] != release["reference_protocol_sha256"]
                or payload["manifest_sha256"] != release["manifest_sha256"]
                or payload["scene"]["scene_id"] != scene_id
            ):
                raise ValueError(
                    "Existing pilot selection/policy differs; use a new output version"
                )
        else:
            payload = prepare(
                manifest_path,
                scene_id,
                Path("data/raw"),
                root,
                policy_path,
                project,
                survey_only=survey_only,
                reference_release=release if survey_only else None,
            )
        write_json(root / "progress.json", summarize(root))
        receipt = log_reference(
            root,
            "sqlite:///" + (project / "data/processed/reference-tracking.sqlite3").as_posix(),
            project / "data/processed/mlflow-artifacts",
        )
        write_json(root / "tracking-receipt.json", receipt)
        print(
            f"{name}: {payload['survey_task_count']} complete-area tasks, {payload['candidate_task_count']} candidate tasks; no automated annotations",
            flush=True,
        )


if __name__ == "__main__":
    main()
