"""Behavioral tests for leakage prevention, matching and frozen data integrity."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import box, mapping

from cryolens.eval.annotations import Annotation, Evidence, ReferencePoint, match_targets
from cryolens.eval.cohort import (
    Exposure,
    SceneRecord,
    assign_partitions,
    coverage_features,
    digest,
    file_digest,
    inventory_ai4arctic,
    load_manifest,
    seal,
    select_partition,
    verify_sources,
    write_frozen_bundle,
)


@pytest.fixture
def protocol() -> dict:
    return dict(json.loads(Path("configs/evaluation/protocol-v1.json").read_text()))


def record(
    name: str, day: int, exposure: Exposure = "metadata_integrity_only", west: float = -54
) -> SceneRecord:
    return SceneRecord(
        scene_id=name,
        acquisition_id=f"acquisition-{name}",
        acquired_utc=datetime(2026, 9, day, tzinfo=UTC).isoformat(),
        family="ai4arctic",
        source_path=f"ai4arctic/{name}.nc",
        source_sha256=digest(name),
        source_bytes=1,
        footprint=mapping(box(west, 48, west + 1, 49)),
        footprint_method="fixture",
        label_status="chart_proxy_available",
        exposure=exposure,
        exposure_reason="fixture",
    )


def payload(records: list[SceneRecord], protocol: dict) -> dict:
    aoi = {
        "type": "Feature",
        "properties": {"id": "newfoundland_labrador_marine"},
        "geometry": mapping(box(-56, 47, -50, 51)),
    }
    return dict(
        json.loads(
            json.dumps(
                {
                    "schema_version": 1,
                    "protocol": protocol,
                    "protocol_sha256": digest(protocol),
                    "aoi": aoi,
                    "aoi_sha256": digest(aoi),
                    "scenes": [
                        r.model_dump(mode="json") for r in assign_partitions(records, protocol)
                    ],
                }
            )
        )
    )


def test_transitive_temporal_group_and_exposure_quarantine(protocol: dict) -> None:
    records = [record("a", 1), record("b", 7), record("c", 13, "analytical"), record("d", 28)]
    result = {r.scene_id: r for r in assign_partitions(records, protocol)}
    assert len({result[n].group_id for n in "abc"}) == 1
    assert all(result[n].partition != "test" for n in "abc")
    assert result["d"].partition == "test"


def test_acquisition_and_content_duplicates_stay_together(protocol: dict) -> None:
    first = record("a", 1)
    same_acquisition = record("b", 28, west=-48).model_copy(
        update={"acquisition_id": first.acquisition_id}
    )
    same_content = record("c", 29, west=-45).model_copy(
        update={"source_sha256": first.source_sha256}
    )
    result = assign_partitions([first, same_acquisition, same_content], protocol)
    assert len({r.group_id for r in result}) == 1
    assert len({r.partition for r in result}) == 1


def test_permutation_stability_and_unknown_never_test(protocol: dict) -> None:
    records = [record("a", 1, "unknown"), record("b", 20), record("c", 29)]
    forward = assign_partitions(records, protocol)
    assert forward == assign_partitions(list(reversed(records)), protocol)
    assert next(r for r in forward if r.scene_id == "a").partition != "test"


def test_manifest_edits_and_resealed_partition_edits_rejected(
    tmp_path: Path, protocol: dict
) -> None:
    data = payload([record("a", 1), record("b", 20, "analytical")], protocol)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(seal(data)))
    assert load_manifest(path) == data
    data["scenes"][0]["partition"] = "train"
    path.write_text(json.dumps({"payload": data, "sha256": "0" * 64}))
    with pytest.raises(ValueError, match="digest mismatch"):
        load_manifest(path)
    path.write_text(json.dumps(seal(data)))
    with pytest.raises(ValueError, match="partitions disagree"):
        load_manifest(path)


def test_source_content_change_rejected_even_with_same_size(tmp_path: Path) -> None:
    path = tmp_path / "ai4arctic/a.nc"
    path.parent.mkdir()
    path.write_bytes(b"a")
    source = record("a", 1).model_copy(update={"source_sha256": file_digest(path)})
    verify_sources([source], tmp_path)
    path.write_bytes(b"b")
    with pytest.raises(ValueError, match="source changed"):
        verify_sources([source], tmp_path)


def test_test_access_release_and_missing_segmentation_truth(protocol: dict) -> None:
    data = payload([record("a", 1), record("b", 20, "analytical")], protocol)
    with pytest.raises(ValueError, match="requires a release"):
        select_partition(data, "test", "target_detection")
    test_ids = [r["scene_id"] for r in data["scenes"] if r["partition"] == "test"]
    release = {
        "manifest_sha256": digest(data),
        "protocol_sha256": data["protocol_sha256"],
        "task": "target_detection",
        "scene_ids": test_ids,
        "model_sha256": "a" * 64,
        "configuration_sha256": "b" * 64,
    }
    assert select_partition(data, "test", "target_detection", release)
    release["scene_ids"] = []
    with pytest.raises(ValueError, match="exact test"):
        select_partition(data, "test", "target_detection", release)
    for item in data["scenes"]:
        item["label_status"] = "publisher_withheld"
    with pytest.raises(ValueError, match="Empty eligible"):
        select_partition(data, "train", "sea_ice_segmentation")


def test_coverage_deduplicates_derivatives_and_never_claims_eligible_area(protocol: dict) -> None:
    first = record("a", 1)
    second = record("b", 1).model_copy(update={"acquisition_id": first.acquisition_id})
    data = payload([first, second], protocol)
    result = coverage_features([SceneRecord.model_validate(r) for r in data["scenes"]], data["aoi"])
    summary = result["summary"]
    assert summary["unique_acquisitions"] == 1
    assert summary["unique_footprint_km2"] == pytest.approx(
        summary["cumulative_acquisition_footprint_km2"]
    )
    assert summary["eligible_surveyed_area_km2"] is None


def test_frozen_bundle_cannot_be_overwritten(tmp_path: Path, protocol: dict) -> None:
    directory = tmp_path / "v1"
    data = payload([record("a", 1)], protocol)
    write_frozen_bundle(directory, data)
    with pytest.raises(FileExistsError):
        write_frozen_bundle(directory, data)
    assert load_manifest(directory / "manifest.json") == data


def test_metadata_inventory_and_duplicate_holdout_rejection(tmp_path: Path, protocol: dict) -> None:
    from netCDF4 import Dataset

    directory = tmp_path / "ai4arctic/train"
    directory.mkdir(parents=True)
    source = directory / "20190119T101218_cis_prep.nc"
    with Dataset(str(source), "w") as dataset:
        dataset.createDimension("row", 2)
        dataset.createDimension("col", 2)
        dataset.original_id = "S1A_EW_GRDM_1SDH_20190119T101218_20190119T101318_025541_02D421_ABCD"
        dataset.createVariable("sar_grid2d_latitude", "f4", ("row", "col"))[:] = [
            [48, 48],
            [49, 49],
        ]
        dataset.createVariable("sar_grid2d_longitude", "f4", ("row", "col"))[:] = [
            [-54, -53],
            [-54, -53],
        ]
        # Inventory works without SAR/chart arrays: metadata is sufficient.
    data = payload([], protocol)
    exposure = {"acquisitions": {}, "family_defaults": {"ai4arctic": "unknown"}}
    records = inventory_ai4arctic(tmp_path, data["aoi"], exposure)
    assert len(records) == 1
    assert records[0].source_sha256 == file_digest(source)
    assert records[0].exposure == "unknown"
    assert records[0].label_status == "no_reference_labels"
    alias = tmp_path / "ai4arctic/copy" / source.name
    alias.parent.mkdir()
    shutil.copyfile(source, alias)
    deduplicated = inventory_ai4arctic(tmp_path, data["aoi"], exposure)
    assert len(deduplicated) == 1
    assert len(deduplicated[0].source_aliases) == 1
    verify_sources(deduplicated, tmp_path)
    duplicate = tmp_path / "evaluation_holdout/ai4arctic" / source.name
    with pytest.raises(ValueError, match="duplicates development"):
        inventory_ai4arctic(tmp_path, data["aoi"], exposure, duplicate)


def point(name: str, lon: float, scene: str = "s") -> ReferencePoint:
    return ReferencePoint(point_id=name, scene_id=scene, longitude=lon, latitude=49)


def test_matching_handles_duplicates_and_different_acquisitions() -> None:
    matches = match_targets(
        [point("p1", -53), point("p2", -53), point("p3", -53, "other")], [point("r", -53)]
    )
    assert len(matches) == 1
    assert matches[0][1:] == ("r", 0.0)
    assert match_targets([point("far", -52)], [point("r", -53)]) == []


def test_matching_maximizes_cardinality_before_distance() -> None:
    # p1 can match either reference; p2 can only match r1. Nearest-first loses a target.
    matches = match_targets(
        [point("p1", -53), point("p2", -53.0019)],
        [point("r1", -53.0002), point("r2", -52.9978)],
        200,
    )
    assert {(p, r) for p, r, _ in matches} == {("p1", "r2"), ("p2", "r1")}


def test_annotation_identity_needs_independent_evidence() -> None:
    when = datetime(2026, 9, 12, tzinfo=UTC)
    kwargs: dict[str, Any] = dict(
        scene_id="s",
        task="target_identification",
        label="iceberg",
        reviewer_id="reviewer",
        reviewed_utc=when,
        acquired_utc=when,
        longitude=-53,
        latitude=49,
        surveyed_area_id="area",
        stage="retained",
        sampling_stratum="all",
        inclusion_probability=1,
    )
    evidence = Evidence(
        kind="sar_review", reference="chip", acquired_utc=when, rationale="Bright return"
    )
    with pytest.raises(ValueError, match="independent"):
        Annotation(**kwargs, evidence=[evidence])
    Annotation(**{**kwargs, "label": "unknown"}, evidence=[evidence])
    with pytest.raises(ValueError, match="does not belong"):
        Annotation(**{**kwargs, "task": "target_detection"}, evidence=[evidence])
