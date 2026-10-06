"""Synthetic reference fixtures; no real human labels or held-out pixels are used."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from shapely.geometry import box, mapping

from cryolens.data.ai4arctic import AI4ArcticScene
from cryolens.detect.cfar import CACFARDetector, GammaCFARDetector
from cryolens.detect.filters import SuppressionConfig, filter_targets
from cryolens.eval.cohort import SceneRecord, assign_partitions, digest, file_digest, seal
from cryolens.geo.vectorize import TargetVectorizer
from cryolens.reference.audit import candidate_frame, tiled_detection
from cryolens.reference.consistency import issue_review
from cryolens.reference.design import ReferencePolicy, finite_population_interval, sample_candidates
from cryolens.reference.freeze import freeze_reference
from cryolens.reference.packet import prepare, read_workspace, write_json
from cryolens.reference.records import HumanResponse, SupportingObservation, events, ingest, ledger
from cryolens.reference.report import summarize


@pytest.fixture
def policy() -> ReferencePolicy:
    return ReferencePolicy.model_validate_json(
        Path("configs/reference/protocol-v1.json").read_text()
    ).model_copy(
        update={
            "survey_core_pixels": 32,
            "survey_context_pixels": 3,
            "candidate_context_pixels": 16,
            "cfar_core_pixels": 32,
            "min_size_samples_per_stratum": 4,
            "other_rejection_samples_per_stratum": 2,
        }
    )


def frame_row(index: int, stage: str | None, size: int = 1) -> dict[str, Any]:
    return {
        "id": f"case-{index}",
        "scene_id": "fixture",
        "first_rejection": stage,
        "pixel_area": size,
        "peak_hv_db": -25.0,
    }


def test_sampling_is_stratified_without_replacement_and_order_independent(
    policy: ReferencePolicy,
) -> None:
    frame = (
        [frame_row(i, "min_size") for i in range(20)]
        + [frame_row(20 + i, "aspect_ratio", 8) for i in range(10)]
        + [frame_row(30 + i, None, 4) for i in range(3)]
    )
    selected, strata = sample_candidates(frame, policy)
    assert sample_candidates(list(reversed(frame)), policy) == (selected, strata)
    assert len(selected) == 9 and len({c["id"] for c in selected}) == 9
    by_stage = {s["first_rejection"]: s for s in strata}
    assert by_stage["min_size"]["selected_count"] == 4
    assert by_stage["min_size"]["inclusion_probability"] == 0.2
    assert by_stage["aspect_ratio"]["selected_count"] == 2
    assert by_stage["retained"]["inclusion_probability"] == 1
    assert sum(s["population_count"] for s in strata) == len(frame)
    with pytest.raises(ValueError, match="Duplicate"):
        sample_candidates(frame + [frame[0]], policy)


def test_finite_population_sampling_uncertainty_does_not_turn_zero_observations_into_certainty() -> (
    None
):
    assert finite_population_interval(20, 20, 0) == (0, 0)
    assert finite_population_interval(20, 20, 6) == (6, 6)
    lower, upper = finite_population_interval(20, 4, 0)
    assert lower == 0 and upper > 0
    lower, upper = finite_population_interval(20, 4, 4)
    assert lower < 20 and upper == 20
    with pytest.raises(ValueError):
        finite_population_interval(20, 0, 0)


@pytest.mark.parametrize("detector_type", [CACFARDetector, GammaCFARDetector])
def test_tiled_cfar_preserves_support_hits_and_clutter_at_boundaries(detector_type: Any) -> None:
    rng = np.random.default_rng(16)
    power = rng.gamma(2, 0.0008, (87, 103))
    hv = 10 * np.log10(power)
    hh = hv + 7
    mask = np.ones(hv.shape, dtype=bool)
    mask[20:29, 27:32] = False
    hv[40, 48] = np.nan
    hv[31:34, 31:34] = -5
    detector = detector_type(background_window=(9, 11), guard_window=(2, 3), pfa=1e-5)
    full = detector.detect(hv, mask, hh, max_hh_hv_ratio_db=None)
    hits, eligible, clutter = tiled_detection(detector, hv, hh, mask, 32)
    np.testing.assert_array_equal(hits, full.detection_mask)
    np.testing.assert_array_equal(eligible, full.analysis_mask)
    np.testing.assert_allclose(clutter, full.clutter_mean_db, atol=1e-8, rtol=0)
    lat, lon = np.indices(hv.shape, dtype=float)
    targets = TargetVectorizer(min_pixels=1).extract_targets(
        hits,
        None,
        hv,
        hh,
        latitude=48 + lat / 10000,
        longitude=-52 + lon / 10000,
        pixel_spacing_m=80,
    )
    assert any(t.pixel_bbox[0] <= 31 and t.pixel_bbox[2] >= 34 for t in targets)


def test_candidate_chip_centre_belongs_to_its_component_when_bounds_contain_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    size = 65
    row, col = np.indices((size, size), dtype=np.float32)
    hv = np.full((size, size), -25, dtype=np.float32)
    hits = np.zeros((size, size), dtype=bool)
    hits[20, 20:41] = hits[40, 20:41] = True
    hits[20:41, 20] = hits[20:41, 40] = True
    hits[30, 30] = True  # An isolated return inside the ring's bounding rectangle.
    scene = AI4ArcticScene(
        "fixture",
        "fixture",
        "fixture",
        80,
        hv + 6,
        hv,
        np.full_like(hv, 35),
        np.full((size, size), 10),
        np.asarray(48 + row / 10000, dtype=np.float32),
        np.asarray(-52 + col / 10000, dtype=np.float32),
    )
    monkeypatch.setattr(
        "cryolens.reference.audit.tiled_detection",
        lambda *args: (hits, np.ones_like(hits), np.full_like(hv, -35)),
    )
    frame, _, _ = candidate_frame(scene, "a" * 64, "gamma", 1e-6, SuppressionConfig(), 32)
    ring = next(candidate for candidate in frame if candidate["pixel_area"] == 80)
    island = next(candidate for candidate in frame if candidate["pixel_area"] == 1)
    assert (island["row"], island["col"]) == (30, 30)
    assert (ring["row"], ring["col"]) != (30, 30)
    assert ring["row"] in {20, 40} or ring["col"] in {20, 40}


def test_candidate_trace_has_one_first_rejection_and_preserves_original_output() -> None:
    hv = np.full((50, 50), -25.0)
    hits = np.zeros(hv.shape, bool)
    hits[10, 10] = True
    hits[30:32, 30:32] = True
    lat, lon = np.indices(hv.shape, dtype=float)
    targets = TargetVectorizer(min_pixels=1).extract_targets(
        hits,
        None,
        hv,
        hv + 6,
        latitude=48 + lat / 10000,
        longitude=-52 + lon / 10000,
        pixel_spacing_m=80,
    )
    configuration = SuppressionConfig(seam_detection_enabled=False, min_peak_hv_db=-24)
    original, stats = filter_targets(targets, configuration)
    decisions: list[dict[str, Any]] = []
    audited, new_stats = filter_targets(targets, configuration, candidate_decisions=decisions)
    assert audited == original and new_stats.as_dict() == stats.as_dict()
    assert [d["first_rejection"] for d in decisions] == ["min_size", "min_peak_hv"]
    assert "min_size" not in decisions[0]["passed_stages"]
    assert "min_size" in decisions[1]["passed_stages"]
    with pytest.raises(ValueError, match="unique"):
        filter_targets(targets + targets, configuration, candidate_decisions=[])


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: ReferencePolicy) -> Path:
    scene_id = "fixture_prep.nc"
    source = tmp_path / "raw" / scene_id
    source.parent.mkdir()
    source.write_bytes(b"explicit synthetic fixture, not satellite data")
    h, w = 65, 67
    row, col = np.indices((h, w), dtype=np.float32)
    hv = np.full((h, w), -25, dtype=np.float32)
    hv[3, 4] = np.nan
    zones = np.full((h, w), 10, dtype=np.int16)
    zones[10:12, 10:12] = 0
    scene = AI4ArcticScene(
        scene_id,
        "fixture-original",
        "fixture",
        80,
        hv + 7,
        hv,
        np.full((h, w), 35, dtype=np.float32),
        zones,
        np.asarray(48 + row / 10000, dtype=np.float32),
        np.asarray(-52 + col / 10000, dtype=np.float32),
    )
    monkeypatch.setattr("cryolens.reference.packet.load_scene", lambda path, load_context: scene)
    monkeypatch.setattr(
        "cryolens.reference.packet.code_provenance",
        lambda project: {"git_commit": "fixture", "git_dirty": False, "source_files_sha256": {}},
    )
    # Select the same frozen feature ID rather than relying on a different jurisdiction polygon.
    from cryolens.eval.cohort import read_aoi

    aoi = read_aoi(Path("configs/aoi.geojson"), "newfoundland_labrador_marine")
    record = {
        "scene_id": scene_id,
        "acquisition_id": "fixture-acquisition",
        "acquired_utc": "2020-01-01T00:00:00+00:00",
        "family": "ai4arctic",
        "source_path": scene_id,
        "source_sha256": file_digest(source),
        "source_bytes": source.stat().st_size,
        "footprint": mapping(box(-52, 48, -51.9, 48.1)),
        "footprint_method": "fixture",
        "label_status": "unknown",
        "exposure": "analytical",
        "exposure_reason": "unit fixture",
        "group_id": "fixture-group",
        "partition": "train",
    }
    manifest = tmp_path / "manifest.json"
    protocol = json.loads(Path("configs/evaluation/protocol-v1.json").read_text())
    write_json(
        manifest,
        seal(
            {
                "schema_version": 1,
                "protocol": protocol,
                "protocol_sha256": digest(protocol),
                "aoi": aoi,
                "aoi_sha256": digest(aoi),
                "scenes": [
                    r.model_dump(mode="json")
                    for r in assign_partitions([SceneRecord.model_validate(record)], protocol)
                ],
            }
        ),
    )
    policy_path = tmp_path / "policy.json"
    write_json(policy_path, policy.model_dump(mode="json"))
    root = tmp_path / "workspace"
    prepare(manifest, scene_id, source.parent, root, policy_path, Path.cwd(), survey_only=True)
    return root


def tasks(root: Path) -> list[dict[str, Any]]:
    return list(json.loads((root / "private/tasks.json").read_text()))


def human(root: Path, task: dict[str, Any], **changes: Any) -> dict[str, Any]:
    metadata = read_workspace(root)
    value = {
        "workspace_id": metadata["workspace_id"],
        "task_id": task["task_id"],
        "phase": "initial",
        "reviewer_id": "fixture-reviewer",
        "reviewed_utc": (
            datetime.fromisoformat(metadata["created_at"]) + timedelta(seconds=1)
        ).isoformat(),
        "status": "complete",
        "active_seconds": 120,
        "personally_inspected": True,
        "viewed_channels": ["HH", "HV"],
        "notes": "Synthetic unit response; not a real satellite annotation",
        "marks": [],
        "excluded_regions": [],
    }
    return {**value, **changes}


def import_rows(
    root: Path, rows: list[dict[str, Any]], name: str = "responses.json"
) -> dict[str, Any]:
    path = root / name
    write_json(path, rows)
    return ingest(root, path, now=datetime.now(UTC) + timedelta(seconds=10))


def test_whole_scene_survey_has_exact_nonoverlapping_cores_and_no_detector_information(
    workspace: Path,
) -> None:
    metadata = read_workspace(workspace)
    assert metadata["detection"] is None and metadata["human_annotations"] == 0
    covered = np.zeros(metadata["source_shape"], dtype=np.uint8)
    for task in tasks(workspace):
        with np.load(workspace / task["grid"]) as grid:
            domain = grid["core"] & grid["survey_domain"]
            row, col = task["origin"]
            h, w = domain.shape
            covered[row : row + h, col : col + w] += domain
    assert covered.max() == 1 and covered.sum() == 65 * 67 - 5
    public = json.loads((workspace / "review/survey/tasks.json").read_text())
    assert all(t["centre"] is None for t in public["tasks"])
    serialized = json.dumps(public)
    assert "first_rejection" not in serialized and "inclusion_probability" not in serialized
    report = summarize(workspace)
    assert report["human_responses"] == 0 and report["target_detection_precision"] is None
    assert report["reference_set_complete"] is False


def test_human_import_is_idempotent_append_only_and_excludes_ambiguity(workspace: Path) -> None:
    task = tasks(workspace)[0]
    with np.load(workspace / task["grid"]) as grid:
        row, col = np.argwhere(grid["core"] & grid["survey_domain"])[50]
    row, col = int(row), int(col)
    mark = {
        "row": row,
        "col": col,
        "label": "uncertain",
        "identity": "unknown",
        "rationale": "Ambiguous compact return in a synthetic unit fixture",
    }
    rows = [human(workspace, task, marks=[mark])]
    assert import_rows(workspace, rows)["inserted_responses"] == 1
    assert import_rows(workspace, rows)["inserted_responses"] == 0
    report = summarize(workspace)
    assert report["reviewers"][0]["surveyed_unambiguous_pixels"] < task["survey_pixels"]
    assert report["reviewers"][0]["annotation_labels"]["uncertain"] == 1
    with pytest.raises(ValueError, match="cannot be overwritten"):
        import_rows(workspace, [{**rows[0], "notes": "Conflicting replacement"}])
    with ledger(workspace) as connection:
        assert len(events(connection)) == 1
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")


def test_invalid_human_labels_positions_and_polarization_attestations_are_rejected(
    workspace: Path,
) -> None:
    task = tasks(workspace)[0]
    invalid = {"row": 3, "col": 4, "label": "target", "rationale": "invalid nodata sample"}
    with pytest.raises(ValueError, match="valid NL offshore"):
        import_rows(workspace, [human(workspace, task, marks=[invalid])])
    with pytest.raises(ValueError, match="both polarizations"):
        HumanResponse.model_validate(human(workspace, task, viewed_channels=["HV"]))
    with pytest.raises(ValueError, match="positive independent evidence"):
        HumanResponse.model_validate(
            human(workspace, task, marks=[{**invalid, "identity": "iceberg"}])
        )
    with pytest.raises(ValueError):
        HumanResponse.model_validate(human(workspace, task, personally_inspected=False))
    assert summarize(workspace)["human_responses"] == 0


def test_optical_identity_requires_positive_clear_hashed_resolution_evidence() -> None:
    value = {
        "kind": "optical",
        "reference": "fixture-source",
        "source_sha256": "a" * 64,
        "acquired_utc": "2020-01-01T00:00:00Z",
        "rationale": "Positive visible object in unit fixture",
        "longitude": -52,
        "latitude": 48,
        "matching_radius_m": 500,
        "position_uncertainty_m": 20,
        "positive_observation": True,
        "visibility": "clear",
        "valid_optical_pixels": True,
        "resolution_m": 10,
    }
    SupportingObservation.model_validate(value)
    for change in (
        {"positive_observation": False},
        {"visibility": "uncertain"},
        {"valid_optical_pixels": False},
        {"resolution_m": None},
        {"source_sha256": "unknown"},
    ):
        with pytest.raises(ValueError):
            SupportingObservation.model_validate({**value, **change})


def test_blind_repeat_waits_one_week_hides_old_answers_and_reports_consistency(
    workspace: Path,
) -> None:
    original = [human(workspace, t) for t in tasks(workspace)]
    import_rows(workspace, original)
    start = datetime.fromisoformat(original[0]["reviewed_utc"])
    with pytest.raises(ValueError, match="not due"):
        issue_review(
            workspace, "fixture-reviewer", "fixture-reviewer", now=start + timedelta(days=6)
        )
    issued = issue_review(
        workspace, "fixture-reviewer", "fixture-reviewer", now=start + timedelta(days=8)
    )
    assert issued["selected_count"] == 2 and issued["repeat_inclusion_probability"] == 2 / 9
    public = json.loads((workspace / issued["folder"] / "tasks.json").read_text())
    assert "first_response_key" not in json.dumps(public)
    assert not ({t["task_id"] for t in tasks(workspace)} & {t["task_id"] for t in public["tasks"]})
    rows = [
        human(
            workspace,
            t,
            phase=issued["phase"],
            reviewed_utc=(start + timedelta(days=8)).isoformat(),
        )
        for t in issued["tasks"]
    ]
    write_json(workspace / "repeat.json", rows)
    ingest(workspace, workspace / "repeat.json", now=start + timedelta(days=9))
    report = summarize(workspace)
    assert report["consistency"][0]["paired_responses"] == 2
    assert report["independent_reviewer_obtained"] is False
    assert report["consistency"][0]["agreement_is_accuracy"] is False


def test_freeze_refuses_missing_humans_then_seals_actual_fixture_records(workspace: Path) -> None:
    now = datetime.now(UTC) + timedelta(seconds=10)
    adjudication = {
        "reviewer_id": "fixture-adjudicator",
        "adjudicated_utc": datetime.now(UTC).isoformat(),
        "notes": "Explicit synthetic fixture adjudication, never a real reference",
        "target_ownership_at_core_boundaries_reviewed": True,
        "independent_reviewer_status": "obtained",
        "case_resolutions": [],
    }
    with pytest.raises(ValueError, match="Every survey"):
        freeze_reference(workspace, "fixture-reviewer", adjudication, workspace / "frozen")
    original = [
        human(
            workspace,
            t,
            reviewed_utc=datetime.fromisoformat(
                read_workspace(workspace)["created_at"]
            ).isoformat(),
        )
        for t in tasks(workspace)
    ]
    import_rows(workspace, original)
    issued = issue_review(
        workspace,
        "fixture-reviewer",
        "independent-fixture-reviewer",
        mode="independent",
        fraction=0.2,
        now=now,
    )
    rows = [
        human(
            workspace,
            t,
            phase=issued["phase"],
            reviewer_id="independent-fixture-reviewer",
            reviewed_utc=now.isoformat(),
        )
        for t in issued["tasks"]
    ]
    write_json(workspace / "independent.json", rows)
    ingest(workspace, workspace / "independent.json", now=now + timedelta(seconds=1))
    # Clock is injected only by the test fixture; this never writes real pilot annotations.
    from unittest.mock import patch

    with patch("cryolens.reference.freeze.datetime") as clock:
        clock.now.return_value = now + timedelta(seconds=2)
        clock.fromisoformat.side_effect = datetime.fromisoformat
        result = freeze_reference(
            workspace,
            "fixture-reviewer",
            {**adjudication, "adjudicated_utc": (now + timedelta(seconds=2)).isoformat()},
            workspace / "frozen",
        )
    assert result["reference_set_complete"] is True and result["surveyed_pixels"] == 65 * 67 - 5
    assert result["detection_eligibility_available"] is False
    assert summarize(workspace)["reference_set_complete"] is True
    (workspace / "frozen/annotations.json").write_text("changed")
    with pytest.raises(ValueError, match="Frozen reference content"):
        summarize(workspace)


def test_workspace_artifact_mutation_is_rejected(workspace: Path) -> None:
    task = tasks(workspace)[0]
    (workspace / "review/survey" / f"{task['task_id']}-HH.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact changed"):
        read_workspace(workspace)


def test_heldout_reference_release_is_distinct_from_model_execution_and_skips_chart_access(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from cryolens.reference import packet

    manifest_path = workspace.parent / "manifest.json"
    payload = json.loads(manifest_path.read_text())["payload"]
    record = SceneRecord.model_validate(payload["scenes"][0]).model_copy(
        update={"exposure": "metadata_integrity_only"}
    )
    payload["scenes"] = [
        r.model_dump(mode="json") for r in assign_partitions([record], payload["protocol"])
    ]
    assert payload["scenes"][0]["partition"] == "test"
    write_json(manifest_path, seal(payload))
    policy_path = workspace.parent / "policy.json"
    policy = ReferencePolicy.model_validate_json(policy_path.read_text())
    reader = Mock(wraps=packet.load_scene)
    monkeypatch.setattr(packet, "load_scene", reader)
    arguments = (
        manifest_path,
        record.scene_id,
        workspace.parent / "raw",
        workspace.parent / "heldout",
        policy_path,
        Path.cwd(),
    )
    with pytest.raises(ValueError, match="source-bound survey-only"):
        prepare(*arguments, survey_only=True)
    reader.assert_not_called()
    release = {
        "purpose": "independent_reference_only",
        "manifest_sha256": digest(payload),
        "reference_protocol_sha256": digest(policy.model_dump(mode="json")),
        "scene_ids": [record.scene_id],
        "detector_execution": False,
        "chart_label_access": False,
        "authorization": "explicit synthetic fixture",
    }
    with pytest.raises(ValueError, match="source-bound survey-only"):
        prepare(*arguments, survey_only=False, reference_release=release)
    reader.assert_not_called()
    result = prepare(*arguments, survey_only=True, reference_release=release)
    assert reader.call_args.kwargs["load_context"] is False
    assert result["reference_only_test_access"] is True
    assert result["chart_label_access"] is False and result["detection"] is None


def test_candidate_review_requires_prior_survey_and_preserves_sampling_weights(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def audit(
        scene: Any, source: str, *args: Any
    ) -> tuple[list[dict[str, Any]], np.ndarray, dict[str, Any]]:
        rows = [
            {
                **frame_row(i, "min_size"),
                "scene_id": scene.scene_id,
                "target_id": i,
                "pixel_bbox": [25, 25 + i, 26, 26 + i],
                "row": 25,
                "col": 25 + i,
                "longitude": -52 + (25 + i) / 10000,
                "latitude": 48.0025,
                "passed_stages": [],
            }
            for i in range(10)
        ]
        return (
            rows,
            np.ones(scene.shape, dtype=bool),
            {"raw_components": 10, "retained_components": 0},
        )

    monkeypatch.setattr("cryolens.reference.packet.candidate_frame", audit)
    root = workspace.parent / "candidate-workspace"
    prepare(
        workspace.parent / "manifest.json",
        "fixture_prep.nc",
        workspace.parent / "raw",
        root,
        workspace.parent / "policy.json",
        Path.cwd(),
        survey_only=False,
    )
    candidate_tasks = [t for t in tasks(root) if t["mode"] == "candidate"]
    assert len(candidate_tasks) == 4

    def answer(task: dict[str, Any], label: str) -> dict[str, Any]:
        mark = {
            "row": task["row"] - task["origin"][0],
            "col": task["col"] - task["origin"][1],
            "label": label,
            "rationale": "Explicit candidate fixture assessment",
        }
        return human(root, task, marks=[mark])

    with pytest.raises(ValueError, match="independent survey dispositions"):
        import_rows(root, [answer(candidate_tasks[0], "target")])
    import_rows(root, [human(root, t) for t in tasks(root) if t["mode"] == "survey"])
    import_rows(
        root,
        [
            answer(t, label)
            for t, label in zip(
                candidate_tasks, ["target", "uncertain", "non_target", "non_target"], strict=True
            )
        ],
        "candidates.json",
    )
    stratum = summarize(root)["reviewers"][0]["candidate_sampling_results"][0]
    assert stratum["inclusion_probability"] == 0.4
    assert stratum["weighted_target_lower"] == 2.5
    assert stratum["weighted_target_upper_if_all_uncertain_are_targets"] == 5
    assert summarize(root)["target_detection_recall"] is None
    peer = issue_review(root, "fixture-reviewer", "peer-fixture", mode="independent", fraction=1)
    # All source metadata and weights stay private in the issued packet.
    public = json.loads((root / peer["folder"] / "tasks.json").read_text())
    assert "first_rejection" not in json.dumps(public)
