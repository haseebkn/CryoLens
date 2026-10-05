"""Real MLflow runs bound to frozen data, code and non-secret configuration."""

from __future__ import annotations

import json
import subprocess
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from cryolens.eval.cohort import (
    Partition,
    SceneRecord,
    Task,
    digest,
    file_digest,
    load_manifest,
    read_aoi,
    select_partition,
    verify_sources,
)

LiteralScope = Literal["scope"]


def code_provenance(project_root: Path) -> dict[str, Any]:
    """Capture commit plus actual code hashes, including dirty/untracked code.

    Only explicit source/config directories are hashed; .env, credentials, Git
    diffs, process environment and settings containing secrets are never logged.
    """

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(project_root), *args], text=True).strip()

    files = {}
    for folder in ("src", "scripts", "configs"):
        for path in sorted((project_root / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".geojson", ".xml"}:
                files[path.relative_to(project_root).as_posix()] = file_digest(path)
    for name in ("pyproject.toml", "uv.lock"):
        path = project_root / name
        if path.is_file():
            files[name] = file_digest(path)
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": bool(
            git(
                "status",
                "--porcelain",
                "--",
                "src",
                "scripts",
                "configs",
                "pyproject.toml",
                "uv.lock",
            )
        ),
        "source_files_sha256": files,
        "code_sha256": digest(files),
        "preprocessing_sha256": digest(
            {
                k: v
                for k, v in files.items()
                if k.startswith(("src/cryolens/preprocess/", "src/cryolens/data/"))
            }
        ),
    }


class TrackedRun:
    """An explicit MLflow run with no process-global active-run state."""

    def __init__(self, client: Any, run_id: str, records: list[SceneRecord]) -> None:
        self.client = client
        self.run_id = run_id
        self.records = records

    def log_metrics(self, metrics: dict[str, float], step: int = 0) -> None:
        """Log measured metrics only; reference-dependent metrics need real truth."""
        for name, value in metrics.items():
            self.client.log_metric(self.run_id, name, float(value), step=step)

    def log_artifact(self, path: Path, artifact_path: str = "results") -> None:
        """Attach an explicit output artifact, never the complete working directory."""
        self.client.log_artifact(self.run_id, str(path), artifact_path)


@contextmanager
def tracked_experiment(
    manifest_path: Path,
    project_root: Path,
    data_root: Path,
    task: Task,
    partition: Partition | LiteralScope,
    configuration: dict[str, Any],
    tracking_uri: str,
    experiment_name: str = "cryolens-nl-v1",
    release: dict[str, Any] | None = None,
    model_path: Path | None = None,
) -> Iterator[TrackedRun]:
    """Verify data and gate test use before starting a real experiment.

    ``scope`` logs the freeze itself, reading no SAR/labels and making no model
    performance claims. Other runs hash selected source files before execution.
    Configuration must be an explicitly constructed non-secret experiment dict.
    """
    from mlflow import MlflowClient

    manifest = load_manifest(manifest_path)
    current_aoi = read_aoi(
        project_root / "configs/aoi.geojson", manifest["protocol"]["aoi_feature_id"]
    )
    if digest(current_aoi) != manifest["aoi_sha256"]:
        raise ValueError("Live AOI differs from frozen scope; create a new protocol version")
    records = [] if partition == "scope" else select_partition(manifest, partition, task, release)
    if records:
        verify_sources(records, data_root)
    if partition == "test":
        if release is None or release["configuration_sha256"] != digest(configuration):
            raise ValueError("Test configuration differs from locked release")
        if model_path is None or file_digest(model_path) != release["model_sha256"]:
            raise ValueError("Test model differs from locked release")
    provenance = code_provenance(project_root)
    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name(experiment_name)
    if experiment is None:
        location = (project_root / "data/processed/mlflow-artifacts").resolve()
        location.mkdir(parents=True, exist_ok=True)
        experiment_id = client.create_experiment(
            experiment_name, artifact_location=location.as_uri()
        )
    else:
        experiment_id = experiment.experiment_id
    run = client.create_run(
        experiment_id,
        tags={
            "mlflow.runName": f"{task}-{partition}",
            "task": task,
            "partition": partition,
            "protocol_id": manifest["protocol"]["protocol_id"],
            "manifest_sha256": digest(manifest),
            "git_commit": provenance["git_commit"],
            "git_dirty": str(provenance["git_dirty"]).lower(),
            "preprocessing_sha256": provenance["preprocessing_sha256"],
            "test_access": str(partition == "test").lower(),
        },
    )
    run_id = run.info.run_id
    try:
        for key, value in {
            "protocol_sha256": manifest["protocol_sha256"],
            "configuration_sha256": digest(configuration),
            "code_sha256": provenance["code_sha256"],
            "selected_scenes": len(records),
            "selected_groups": len({r.group_id for r in records}),
        }.items():
            client.log_param(run_id, key, str(value))
        with TemporaryDirectory(prefix="cryolens-tracking-") as temporary:
            snapshot = Path(temporary) / "code_snapshot.zip"
            with zipfile.ZipFile(snapshot, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, expected in provenance["source_files_sha256"].items():
                    source = project_root / name
                    if file_digest(source) != expected:
                        raise ValueError("Code changed while recording experiment provenance")
                    archive.write(source, name)
            client.log_artifact(run_id, str(snapshot), "provenance")
            for name, value in {
                "dataset_manifest": json.loads(manifest_path.read_text(encoding="utf-8")),
                "protocol": manifest["protocol"],
                "configuration": configuration,
                "code_provenance": provenance,
                "selection": [r.model_dump(mode="json") for r in records],
                "test_release": release,
            }.items():
                path = Path(temporary) / f"{name}.json"
                path.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
                client.log_artifact(run_id, str(path), "provenance")
        yield TrackedRun(client, run_id, records)
    except BaseException:
        client.set_terminated(run_id, status="FAILED")
        raise
    else:
        client.set_terminated(run_id, status="FINISHED")
