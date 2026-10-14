"""Real local MLflow registry, signatures, research aliases and rollback evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from cryolens.eval.cohort import digest, file_digest
from cryolens.segmentation.data import write_json


def demonstrate(training: Path, output: Path) -> dict[str, Any]:
    """Register actual trained artifacts; no operational performance claim is made."""
    import mlflow
    from mlflow import MlflowClient
    from mlflow.models import ModelSignature
    from mlflow.pyfunc import PythonModel
    from mlflow.types import Schema, TensorSpec

    comparison = json.loads((training / "comparison.json").read_text())
    config = json.loads((training / "configuration.json").read_text())
    if comparison["test_access"] or not comparison["development_only"]:
        raise ValueError("Expected a completed development-only comparison")
    output.mkdir(parents=True, exist_ok=False)
    mlflow.set_tracking_uri(config["tracking_uri"])
    client = MlflowClient(tracking_uri=config["tracking_uri"])
    experiment = client.get_experiment_by_name("cryolens-seaice-development-v1")
    if experiment is None:
        raise ValueError("Actual training experiment is missing")
    name = (
        "cryolens-seaice-research-"
        + comparison["configuration_sha256"][:12]
        + "-"
        + digest(output.name)[:8]
    )
    if any(m.name == name for m in client.search_registered_models()):
        raise ValueError("Research registry name occupied; use a new versioned experiment")
    client.create_registered_model(name, tags={"research_only": "true", "test_access": "false"})
    signature = ModelSignature(
        inputs=Schema([TensorSpec(np.dtype(np.float32), (-1, 2, 128, 128))]),
        outputs=Schema([TensorSpec(np.dtype(np.float32), (-1, 2, 128, 128))]),
    )

    class ChartSegmenter(PythonModel):
        def load_context(self, context: Any) -> None:
            import torch

            self.model = torch.jit.load(context.artifacts["weights"], map_location="cpu").eval()

        def predict(self, context: Any, model_input: Any, params: Any = None) -> Any:
            import numpy as np
            import torch

            x = np.asarray(model_input)
            if (
                x.dtype != np.float32
                or x.ndim != 4
                or x.shape[1:] != (2, 128, 128)
                or not np.isfinite(x).all()
            ):
                raise ValueError("Expected finite float32 physical-dB [N,2,128,128]")
            with torch.no_grad():
                return self.model(torch.from_numpy(x)).numpy().astype(np.float32)

    for filename, expected in comparison["models"]["unet"]["inference_artifacts"].items():
        artifact = (training / "unet" / filename).resolve()
        if (
            not artifact.is_relative_to((training / "unet").resolve())
            or file_digest(artifact) != expected
        ):
            raise ValueError("Trained inference artifact changed")
    first = training / "unet/epoch1-inference.pt"
    candidate = training / "unet/best-inference.pt"
    if file_digest(first) == file_digest(candidate):
        candidate = training / "unet/final-inference.pt"
    versions = []
    with np.load(Path(config["dataset_root"]) / "validation.npz", allow_pickle=False) as z:
        example = z["inputs_db"][:1].astype(np.float32)
    np.save(output / "request.npy", example, allow_pickle=False)
    for path in [first, candidate]:
        with mlflow.start_run(
            experiment_id=experiment.experiment_id, run_name="research-package-" + path.stem
        ) as active:
            mlflow.set_tags(
                {
                    "research_only": "true",
                    "test_access": "false",
                    "training_run_id": comparison["models"]["unet"]["run_id"],
                    "model_sha256": file_digest(path),
                    "manifest_sha256": comparison["manifest_sha256"],
                }
            )
            info = mlflow.pyfunc.log_model(
                name="chart-model",
                python_model=ChartSegmenter(),
                artifacts={"weights": str(path)},
                signature=signature,
                pip_requirements=[
                    "mlflow-skinny==" + mlflow.__version__,
                    "numpy==" + np.__version__,
                    "torch==2.8.0",
                ],
            )
            version = client.create_model_version(
                name,
                source=info.model_uri,
                run_id=active.info.run_id,
                tags={
                    "model_sha256": file_digest(path),
                    "research_only": "true",
                    "test_access": "false",
                },
            )
            loaded = mlflow.pyfunc.load_model(f"models:/{name}/{version.version}")
            prediction = loaded.predict(example)
            if (
                prediction.shape != (1, 2, 128, 128)
                or not np.isfinite(prediction).all()
                or not np.allclose(prediction.sum(1), 1, atol=1e-5)
            ):
                raise ValueError("Registered model failed inference contract")
            np.save(
                output / ("version-" + str(version.version) + "-expected.npy"),
                prediction,
                allow_pickle=False,
            )
            versions.append(
                {
                    "version": str(version.version),
                    "run_id": active.info.run_id,
                    "model_uri": info.model_uri,
                    "artifact": str(path),
                    "model_sha256": file_digest(path),
                    "signature_verified": True,
                }
            )
    # Research aliases describe software lifecycle, not maritime deployment permission.
    transitions = []
    for label, selected_version in [
        ("initial", versions[0]),
        ("promotion", versions[1]),
        ("rollback", versions[0]),
    ]:
        client.set_registered_model_alias(name, "research-champion", selected_version["version"])
        resolved = client.get_model_version_by_alias(name, "research-champion")
        if str(resolved.version) != str(selected_version["version"]):
            raise ValueError("Alias transition failed")
        pred = mlflow.pyfunc.load_model(f"models:/{name}@research-champion").predict(example)
        expected = np.load(
            output / ("version-" + str(resolved.version) + "-expected.npy"), allow_pickle=False
        )
        if not np.allclose(pred, expected, atol=1e-6):
            raise ValueError("Alias serving did not resolve the qualified artifact")
        transitions.append(
            {
                "action": label,
                "alias": "research-champion",
                "version": str(resolved.version),
                "model_sha256": resolved.tags["model_sha256"],
                "prediction_verified": True,
            }
        )
    client.set_registered_model_alias(name, "research-candidate", versions[1]["version"])
    result = {
        "schema_version": 1,
        "registered_model": name,
        "tracking_uri": config["tracking_uri"],
        "research_only": True,
        "test_access": False,
        "training_run_id": comparison["models"]["unet"]["run_id"],
        "versions": versions,
        "transitions": transitions,
        "signature": signature.to_dict(),
        "container_demonstration_complete": False,
        "claim_boundary": "Actual local research lifecycle, not operational staged maritime deployment or automatic retraining",
    }
    write_json(output / "registry.json", result)
    return result
