"""Native review packets with hidden selection information and exact survey grids."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from cryolens.data.ai4arctic import AI4ArcticScene, load_scene
from cryolens.detect.filters import SuppressionConfig
from cryolens.eval.cohort import (
    SceneRecord,
    digest,
    file_digest,
    load_manifest,
    read_aoi,
    verify_sources,
)
from cryolens.eval.tracking import code_provenance
from cryolens.geo.aoi import points_in_aoi
from cryolens.reference.audit import candidate_frame
from cryolens.reference.design import ReferencePolicy, sample_candidates


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")


def read_workspace(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    payload: dict[str, Any] = manifest["payload"]
    if manifest["sha256"] != digest(payload):
        raise ValueError("Reference workspace manifest changed")
    for name, expected in payload["files_sha256"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or file_digest(path) != expected:
            raise ValueError("Reference artifact changed or escapes the workspace")
    return payload


def install_viewer(folder: Path, tasks: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    static = Path(__file__).parents[1] / "web/static"
    for source, destination in (
        ("reference.html", "index.html"),
        ("reference.js", "reference.js"),
        ("reference.css", "reference.css"),
    ):
        shutil.copyfile(static / source, folder / destination)
    # Reviewer packet deliberately contains neither strata nor controller candidate IDs.
    write_json(folder / "tasks.json", {"schema_version": 1, **metadata, "tasks": tasks})


def _image(path: Path, values: np.ndarray, valid: np.ndarray, limits: tuple[float, float]) -> None:
    scaled = np.clip(
        (np.nan_to_num(values, nan=limits[0]) - limits[0]) / (limits[1] - limits[0]), 0, 1
    )
    rgb = np.repeat(np.asarray(scaled * 255, dtype=np.uint8)[..., None], 3, axis=-1)
    rgb[~valid] = [45, 55, 65]
    Image.fromarray(rgb).save(path)


def make_task(
    scene: AI4ArcticScene,
    domain: np.ndarray,
    detector_eligible: np.ndarray | None,
    root: Path,
    folder: Path,
    task_id: str,
    core_bounds: list[int],
    context: int,
    mode: str,
    extra: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Core partitions never overlap; context is display-only and cannot be annotated."""
    r0, c0, r1, c1 = core_bounds
    h, w = scene.shape
    a, b, z, d = (
        max(0, r0 - context),
        max(0, c0 - context),
        min(h, r1 + context),
        min(w, c1 + context),
    )
    view = np.s_[a:z, b:d]
    core = np.zeros((z - a, d - b), dtype=bool)
    core[r0 - a : r1 - a, c0 - b : c1 - b] = True
    grid_path = root / "private" / f"{task_id}.npz"
    np.savez_compressed(
        grid_path,
        latitude=scene.latitude[view],
        longitude=scene.longitude[view],
        survey_domain=domain[view],
        core=core,
        detector_eligible=np.zeros_like(core)
        if detector_eligible is None
        else detector_eligible[view],
    )
    folder.mkdir(parents=True, exist_ok=True)
    for band, values, limits in (
        ("HH", scene.sigma0_hh_db, (-30.0, -5.0)),
        ("HV", scene.sigma0_hv_db, (-40.0, -10.0)),
    ):
        _image(folder / f"{task_id}-{band}.png", values[view], domain[view], limits)
    private = {
        "task_id": task_id,
        "mode": mode,
        "scene_id": scene.scene_id,
        "origin": [a, b],
        "core_bounds": core_bounds,
        "grid": grid_path.relative_to(root).as_posix(),
        "survey_pixels": int(np.count_nonzero(domain[view] & core)),
        **extra,
    }
    public = {
        "task_id": task_id,
        "mode": mode,
        "acquired_utc": extra["acquired_utc"],
        "spacing_m": scene.pixel_spacing_m,
        "shape": list(core.shape),
        "core": [r0 - a, c0 - b, r1 - a, c1 - b],
        "HH": f"{task_id}-HH.png",
        "HV": f"{task_id}-HV.png",
        "centre": None if mode == "survey" else [extra["row"] - a, extra["col"] - b],
        "optical": [],
    }
    return private, public


def attach_optical(
    root: Path, folder: Path, tasks: list[dict[str, Any]], mappings: list[dict[str, Any]]
) -> None:
    """Copy verified existing pairs for matching development candidates, without review answers.

    Native optical hashes and visibility remain explicit. This is supporting
    context, never generated ground truth or a new optical classification.
    """
    from pyproj import Geod

    from cryolens.review.pairs import verify_pair_files

    geod = Geod(ellps="WGS84")
    optical_root = root.parents[1] / "optical-review"
    if not optical_root.is_dir():
        # Workspaces need not use the conventional processed/reference/<name> layout.
        from cryolens.config.settings import get_settings

        optical_root = get_settings().data_dir / "processed/optical-review"
    pairs = []
    for path in optical_root.glob("*/pair.json"):
        pair = json.loads(path.read_text(encoding="utf-8"))
        if pair.get("optical_item_id"):
            pairs.append((path, pair))
    copied: set[str] = set()
    for public, private in zip(tasks, mappings, strict=True):
        candidate = private.get("candidate")
        if candidate is None:
            continue
        for path, pair in pairs:
            if pair["radar"]["source_sha256"] != private["source_sha256"]:
                continue
            lon, lat = pair["candidate_lonlat"]
            if geod.inv(lon, lat, candidate["longitude"], candidate["latitude"])[2] > 80:
                continue
            verify_pair_files(path.parent, pair)
            file_name = f"optical-{pair['id']}.png"
            if file_name not in copied:
                shutil.copyfile(path.parent / "paired.png", folder / file_name)
                copied.add(file_name)
            public["optical"].append(
                {
                    "image": file_name,
                    "pair_id": pair["id"],
                    "provider": pair.get("optical_provider", "planetary-computer"),
                    "item_id": pair["optical_item_id"],
                    "acquired_utc": pair["optical_acquired_utc"],
                    "signed_time_separation_seconds": pair["signed_time_separation_seconds"],
                    "visibility": pair["visibility"],
                    "full_movement_envelope_in_chip": pair["full_movement_envelope_in_chip"],
                    "source_manifest_sha256": file_digest(path),
                    "evidence_use": "context requiring analyst inspection; optical absence never establishes a false target",
                }
            )


def prepare(
    manifest_path: Path,
    scene_id: str,
    data_root: Path,
    output: Path,
    policy_path: Path,
    project_root: Path,
    survey_only: bool = False,
    reference_release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    policy = ReferencePolicy.model_validate_json(policy_path.read_text())
    record = next(
        (SceneRecord.model_validate(r) for r in manifest["scenes"] if r["scene_id"] == scene_id),
        None,
    )
    if record is None or record.family != "ai4arctic":
        raise ValueError(
            "Reference pilot requires a frozen AI4Arctic input; fresh SAFE release remains blocked"
        )
    if (
        digest(
            read_aoi(project_root / "configs/aoi.geojson", manifest["protocol"]["aoi_feature_id"])
        )
        != manifest["aoi_sha256"]
    ):
        raise ValueError("NL study polygon differs from the frozen manifest")
    if record.partition == "test":
        expected = {
            "purpose": "independent_reference_only",
            "manifest_sha256": digest(manifest),
            "reference_protocol_sha256": digest(policy.model_dump(mode="json")),
            "scene_ids": [scene_id],
            "detector_execution": False,
            "chart_label_access": False,
        }
        if (
            not survey_only
            or reference_release is None
            or any(reference_release.get(k) != v for k, v in expected.items())
            or not reference_release.get("authorization")
        ):
            raise ValueError(
                "Held-out reference access requires a source-bound survey-only release; no detector or chart access"
            )
    verify_sources([record], data_root)
    provenance = code_provenance(project_root)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Refusing to overwrite a reference workspace")
    (output / "private").mkdir(parents=True, exist_ok=True)
    # Write the test access receipt before pixel reads, including failed preparations.
    access = {
        "started_at": datetime.now(UTC).isoformat(),
        "scene": record.model_dump(mode="json"),
        "reference_only_test_access": record.partition == "test",
        "model_test_evaluation_access": False,
        "chart_label_access": False if survey_only else True,
        "reference_release": reference_release,
        "purpose": "human reference preparation, no automated target labels",
    }
    write_json(output / "access.json", access)
    scene = load_scene(data_root / record.source_path, load_context=not survey_only)
    if scene.scene_id != scene_id:
        raise ValueError("Decoded scene identity differs from frozen input")
    domain = (
        scene.valid_mask
        & (scene.land_distance_zone > 2)
        & points_in_aoi(scene.longitude, scene.latitude)
    )
    if not domain.any():
        raise ValueError("No valid offshore SAR pixels in the NL study polygon")
    frame: list[dict[str, Any]] = []
    eligible: np.ndarray | None = None
    detection: dict[str, Any] | None = None
    configuration = {
        "detector": "gamma",
        "pfa": 1e-6,
        "suppression": asdict(SuppressionConfig(exclude_sea_ice=True)),
    }
    if not survey_only:
        frame, eligible, detection = candidate_frame(
            scene,
            record.source_sha256,
            "gamma",
            1e-6,
            SuppressionConfig(exclude_sea_ice=True),
            policy.cfar_core_pixels,
        )
    selected, strata = sample_candidates(frame, policy)
    write_json(output / "private/frame.json", frame)
    identity = digest(
        {
            "manifest": digest(manifest),
            "source": record.source_sha256,
            "policy": policy.model_dump(mode="json"),
            "configuration": configuration if not survey_only else None,
            "generating_code_sha256": digest(provenance["source_files_sha256"]),
            "viewer_sha256": {
                name: file_digest(Path(__file__).parents[1] / "web/static" / name)
                for name in ("reference.html", "reference.js", "reference.css")
            },
        }
    )
    mappings, survey_tasks, candidate_tasks = [], [], []
    common = {
        "acquired_utc": record.acquired_utc,
        "source_sha256": record.source_sha256,
        "group_id": record.group_id,
    }
    h, w = scene.shape
    for row in range(0, h, policy.survey_core_pixels):
        for col in range(0, w, policy.survey_core_pixels):
            bounds = [
                row,
                col,
                min(h, row + policy.survey_core_pixels),
                min(w, col + policy.survey_core_pixels),
            ]
            if not domain[row : bounds[2], col : bounds[3]].any():
                continue
            task_id = digest({"workspace": identity, "survey_core": bounds})[:32]
            private, public = make_task(
                scene,
                domain,
                eligible,
                output,
                output / "review/survey",
                task_id,
                bounds,
                policy.survey_context_pixels,
                "survey",
                {
                    **common,
                    "inclusion_probability": 1.0,
                    "sampling_stratum": "whole_scene_offshore_survey",
                    "stage": "independent_search",
                },
            )
            mappings.append(private)
            survey_tasks.append(public)
    candidate_mappings = []
    for candidate in selected:
        row, col = candidate["row"], candidate["col"]
        task_id = digest({"workspace": identity, "candidate": candidate["id"]})[:32]
        bounds = [
            max(0, row - policy.candidate_context_pixels),
            max(0, col - policy.candidate_context_pixels),
            min(h, row + policy.candidate_context_pixels + 1),
            min(w, col + policy.candidate_context_pixels + 1),
        ]
        private, public = make_task(
            scene,
            domain,
            eligible,
            output,
            output / "review/candidates",
            task_id,
            bounds,
            0,
            "candidate",
            {
                **common,
                **candidate,
                "candidate": candidate,
                "stage": candidate["first_rejection"] or "retained",
            },
        )
        mappings.append(private)
        candidate_mappings.append(private)
        candidate_tasks.append(public)
    attach_optical(output, output / "review/candidates", candidate_tasks, candidate_mappings)
    metadata = {
        "workspace_id": identity,
        "protocol_id": policy.protocol_id,
        "phase": "initial",
        "assignment": None,
        "not_before_utc": None,
        "identity_claim_boundary": "Observable SAR targets are distinct from corroborated ship/iceberg identities. Missing optical counterparts are not negatives.",
    }
    install_viewer(
        output / "review/survey",
        survey_tasks,
        {**metadata, "queue": "Independent complete-area survey"},
    )
    if candidate_tasks:
        install_viewer(
            output / "review/candidates",
            candidate_tasks,
            {**metadata, "queue": "Candidate review after independent survey"},
        )
    write_json(output / "private/tasks.json", mappings)
    with zipfile.ZipFile(
        output / "private/code-snapshot.zip", "w", compression=zipfile.ZIP_DEFLATED
    ) as snapshot:
        for name, expected in provenance["source_files_sha256"].items():
            path = project_root / name
            if file_digest(path) != expected:
                raise ValueError("Code changed during reference preparation")
            snapshot.write(path, name)
    files = {
        p.relative_to(output).as_posix(): file_digest(p)
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    payload = {
        "schema_version": 1,
        "workspace_id": identity,
        "created_at": datetime.now(UTC).isoformat(),
        "scene": record.model_dump(mode="json"),
        "manifest_sha256": digest(manifest),
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": digest(policy.model_dump(mode="json")),
        "configuration": configuration if not survey_only else None,
        "code": provenance,
        "source_shape": list(scene.shape),
        "normalization": scene.assumptions,
        "survey_task_count": len(survey_tasks),
        "survey_pixels": int(domain.sum()),
        "nominal_pixel_area_km2": scene.pixel_area_km2(),
        "approximate_available_survey_area_km2": float(domain.sum()) * scene.pixel_area_km2(),
        "candidate_task_count": len(candidate_tasks),
        "sample_strata": strata,
        "detection": detection,
        "reference_only_test_access": record.partition == "test",
        "chart_label_access": not survey_only,
        "whole_scene_survey_complete": False,
        "human_annotations": 0,
        "files_sha256": files,
    }
    write_json(output / "workspace.json", {"payload": payload, "sha256": digest(payload)})
    return payload
