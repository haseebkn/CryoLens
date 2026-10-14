"""Track prepared reference provenance and counts, without inventing model metrics."""

from pathlib import Path
from typing import Any

from cryolens.reference.packet import read_workspace
from cryolens.reference.report import summarize


def log_reference(root: Path, tracking_uri: str, artifact_root: Path) -> dict[str, Any]:
    from mlflow import MlflowClient

    workspace = read_workspace(root)
    client = MlflowClient(tracking_uri=tracking_uri)
    name = "cryolens-independent-reference-v1"
    experiment = client.get_experiment_by_name(name)
    artifact_root.mkdir(parents=True, exist_ok=True)
    experiment_id = (
        experiment.experiment_id
        if experiment
        else client.create_experiment(name, artifact_location=artifact_root.resolve().as_uri())
    )
    run = client.create_run(
        experiment_id,
        tags={
            "mlflow.runName": "human-reference-preparation",
            "workspace_id": workspace["workspace_id"],
            "git_commit": workspace["code"]["git_commit"],
            "git_dirty": str(workspace["code"]["git_dirty"]).lower(),
            "reference_only_test_access": str(workspace["reference_only_test_access"]).lower(),
            "model_evaluation": "false",
        },
    )
    try:
        for name, value in {
            "manifest_sha256": workspace["manifest_sha256"],
            "reference_protocol_sha256": workspace["policy_sha256"],
            "source_sha256": workspace["scene"]["source_sha256"],
            "partition": workspace["scene"]["partition"],
            "survey_tasks_prepared": workspace["survey_task_count"],
            "candidate_tasks_prepared": workspace["candidate_task_count"],
            "human_responses": summarize(root)["human_responses"],
        }.items():
            client.log_param(run.info.run_id, name, str(value))
        for path in (
            root / "workspace.json",
            root / "access.json",
            root / "private/code-snapshot.zip",
        ):
            client.log_artifact(run.info.run_id, str(path), "reference-provenance")
    except BaseException:
        client.set_terminated(run.info.run_id, "FAILED")
        raise
    client.set_terminated(run.info.run_id, "FINISHED")
    return {
        "run_id": run.info.run_id,
        "experiment_id": experiment_id,
        "workspace_id": workspace["workspace_id"],
        "model_metrics_logged": False,
    }
