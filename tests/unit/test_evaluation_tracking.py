"""Exercise real MLflow persistence, provenance and failure status locally."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mlflow")

from mlflow import MlflowClient

from cryolens.eval.cohort import SceneRecord, assign_partitions, digest, file_digest, seal
from cryolens.eval.tracking import tracked_experiment


@pytest.fixture
def scope(tmp_path: Path) -> tuple[Path, str]:
    protocol = json.loads(Path("configs/evaluation/protocol-v1.json").read_text())
    aoi = json.loads(Path("configs/aoi.geojson").read_text())["features"][0]
    payload = {
        "schema_version": 1,
        "protocol": protocol,
        "protocol_sha256": digest(protocol),
        "aoi": aoi,
        "aoi_sha256": digest(aoi),
        "scenes": [],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(seal(payload)))
    return path, "sqlite:///" + (tmp_path / "mlflow.db").as_posix()


def test_real_tracking_logs_manifest_configuration_code_and_no_performance(
    scope: tuple[Path, str],
) -> None:
    path, uri = scope
    with tracked_experiment(
        path,
        Path.cwd(),
        Path("data/raw"),
        "target_detection",
        "scope",
        {"purpose": "freeze"},
        uri,
        "test-freeze",
    ) as run:
        run_id = run.run_id
    client = MlflowClient(tracking_uri=uri)
    result = client.get_run(run_id)
    assert result.info.status == "FINISHED"
    assert result.data.tags["test_access"] == "false"
    assert len(result.data.tags["git_commit"]) == 40
    assert result.data.params["configuration_sha256"] == digest({"purpose": "freeze"})
    assert result.data.metrics == {}
    artifacts = {a.path for a in client.list_artifacts(run_id, "provenance")}
    assert {
        "provenance/dataset_manifest.json",
        "provenance/code_provenance.json",
        "provenance/protocol.json",
        "provenance/configuration.json",
        "provenance/code_snapshot.zip",
    } <= artifacts
    downloaded = client.download_artifacts(run_id, "provenance/code_provenance.json")
    provenance = json.loads(Path(downloaded).read_text())
    assert ".env" not in provenance["source_files_sha256"]
    assert provenance["preprocessing_sha256"]


def test_failed_experiment_is_persisted_as_failed(scope: tuple[Path, str]) -> None:
    path, uri = scope
    with pytest.raises(RuntimeError, match="deliberate failure"):
        with tracked_experiment(
            path, Path.cwd(), Path("data/raw"), "target_detection", "scope", {}, uri, "test-failure"
        ) as run:
            run_id = run.run_id
            raise RuntimeError("deliberate failure")
    assert MlflowClient(tracking_uri=uri).get_run(run_id).info.status == "FAILED"


def test_test_access_rejected_before_tracking_creates_run(scope: tuple[Path, str]) -> None:
    path, uri = scope
    with pytest.raises(ValueError, match="requires a release"):
        with tracked_experiment(
            path, Path.cwd(), Path("data/raw"), "target_detection", "test", {}, uri, "test-denied"
        ):
            pytest.fail("Unreleased test must not execute")


def test_test_release_checks_actual_model_and_configuration(
    scope: tuple[Path, str], tmp_path: Path
) -> None:
    path, uri = scope
    manifest = json.loads(path.read_text())["payload"]
    source = tmp_path / "source.nc"
    source.write_bytes(b"dataset-content")
    record = SceneRecord(
        scene_id="reserved",
        acquisition_id="original",
        acquired_utc="2026-09-12T00:00:00+00:00",
        family="ai4arctic",
        source_path=source.name,
        source_sha256=file_digest(source),
        source_bytes=source.stat().st_size,
        footprint=manifest["aoi"]["geometry"],
        footprint_method="fixture",
        label_status="chart_proxy_available",
        exposure="metadata_integrity_only",
        exposure_reason="fixture",
    )
    manifest["scenes"] = [
        r.model_dump(mode="json") for r in assign_partitions([record], manifest["protocol"])
    ]
    path.write_text(json.dumps(seal(manifest)))
    model = tmp_path / "model.dat"
    model.write_bytes(b"locked-model")
    configuration = {"threshold": 0.5}
    release = {
        "manifest_sha256": digest(manifest),
        "protocol_sha256": manifest["protocol_sha256"],
        "scene_ids": ["reserved"],
        "task": "sea_ice_segmentation",
        "model_sha256": file_digest(model),
        "configuration_sha256": digest(configuration),
    }
    with pytest.raises(ValueError, match="configuration differs"):
        with tracked_experiment(
            path,
            Path.cwd(),
            tmp_path,
            "sea_ice_segmentation",
            "test",
            {"threshold": 0.8},
            uri,
            release=release,
            model_path=model,
        ):
            pytest.fail("Changed configuration must not execute")
    model.write_bytes(b"changed-model")
    with pytest.raises(ValueError, match="model differs"):
        with tracked_experiment(
            path,
            Path.cwd(),
            tmp_path,
            "sea_ice_segmentation",
            "test",
            configuration,
            uri,
            release=release,
            model_path=model,
        ):
            pytest.fail("Changed model must not execute")
    model.write_bytes(b"locked-model")
    with tracked_experiment(
        path,
        Path.cwd(),
        tmp_path,
        "sea_ice_segmentation",
        "test",
        configuration,
        uri,
        release=release,
        model_path=model,
    ) as run:
        assert len(run.records) == 1
        run_id = run.run_id
    persisted = MlflowClient(tracking_uri=uri).get_run(run_id)
    assert persisted.info.status == "FINISHED"
    assert persisted.data.tags["test_access"] == "true"
