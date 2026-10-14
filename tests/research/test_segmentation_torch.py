"""Executed CPU inference and model-training behavior; requires research extra."""

import hashlib
import io
from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")
from fastapi.testclient import TestClient  # noqa: E402

from cryolens.segmentation.models import InferenceWrapper, LinearHead, UNet  # noqa: E402
from cryolens.segmentation.service import create_app  # noqa: E402


def test_unet_learns_and_linear_probe_keeps_spatial_contract() -> None:
    torch.set_num_threads(2)
    torch.manual_seed(3)
    model = UNet()
    x = torch.randn(2, 2, 32, 32)
    y = (x[:, 0] > 0).long()
    opt = torch.optim.Adam(model.parameters(), lr=0.01)
    before = torch.nn.functional.cross_entropy(model(x), y).item()
    for _ in range(8):
        opt.zero_grad()
        loss = torch.nn.functional.cross_entropy(model(x), y)
        loss.backward()
        opt.step()
    assert torch.nn.functional.cross_entropy(model(x), y).item() < before
    assert LinearHead()(torch.zeros(1, 768, 16, 16)).shape == (1, 2, 128, 128)


def test_real_service_rejects_bad_shape_and_tampered_model(tmp_path: Path) -> None:
    model = InferenceWrapper(UNet().eval()).eval()
    path = tmp_path / "model.pt"
    torch.jit.trace(model, torch.zeros(1, 2, 128, 128)).save(str(path))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="checksum"):
        create_app(path, "0" * 64)
    client = TestClient(create_app(path, sha))
    assert client.get("/metadata").json()["research_only"] is True
    stream = io.BytesIO()
    np.save(stream, np.zeros((1, 2, 128, 128), dtype=np.float32))
    response = client.post("/segment", content=stream.getvalue())
    assert response.status_code == 200 and response.headers["X-Model-Sha256"] == sha
    output = np.load(io.BytesIO(response.content), allow_pickle=False)
    np.testing.assert_allclose(output.sum(1), 1, atol=1e-5)
    assert client.post("/segment", content=b"not-npy").status_code == 422
    stream = io.BytesIO()
    np.save(stream, np.full((1, 2, 128, 128), np.nan, dtype=np.float32))
    assert client.post("/segment", content=stream.getvalue()).status_code == 422


def test_hostile_npy_header_rejected_before_data_allocation(tmp_path: Path) -> None:
    path = tmp_path / "safe.pt"
    torch.jit.trace(InferenceWrapper(UNet().eval()).eval(), torch.zeros(1, 2, 128, 128)).save(
        str(path)
    )
    client = TestClient(create_app(path, hashlib.sha256(path.read_bytes()).hexdigest()))
    header = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        header, {"descr": "<f4", "fortran_order": False, "shape": (1000000000, 2, 128, 128)}
    )
    assert client.post("/segment", content=header.getvalue()).status_code == 422


def test_real_registry_signature_promotion_and_rollback(tmp_path: Path) -> None:
    """Synthetic models, real SQLite/MLflow API; versions may be int or str."""
    import json

    from mlflow import MlflowClient

    from cryolens.segmentation.lifecycle import demonstrate

    training = tmp_path / "training"
    weights = training / "unet"
    weights.mkdir(parents=True)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    example = np.zeros((1, 2, 128, 128), dtype=np.float32)
    np.savez_compressed(dataset / "validation.npz", inputs_db=example)
    artifacts = {}
    for index, name in enumerate(["epoch1", "best", "final"]):
        torch.manual_seed(index)
        path = weights / (name + "-inference.pt")
        wrapped = InferenceWrapper(torch.nn.Conv2d(2, 2, 1).eval()).eval()
        torch.jit.trace(wrapped, torch.from_numpy(example)).save(str(path))
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    uri = "sqlite:///" + str(tmp_path / "registry.db").replace("\\", "/")
    client = MlflowClient(tracking_uri=uri)
    experiment_id = client.create_experiment(
        "cryolens-seaice-development-v1", artifact_location=(tmp_path / "artifacts").as_uri()
    )
    training_run = client.create_run(experiment_id)
    client.set_terminated(training_run.info.run_id)
    from cryolens.eval.cohort import digest

    config = {"tracking_uri": uri, "dataset_root": str(dataset)}
    (training / "configuration.json").write_text(json.dumps(config))
    (dataset / "dataset.json").write_text(
        json.dumps(
            {
                "test_access": False,
                "manifest_sha256": "b" * 64,
                "configuration_sha256": digest(config),
                "partitions": {
                    "validation": {
                        "file": "validation.npz",
                        "sha256": hashlib.sha256(
                            (dataset / "validation.npz").read_bytes()
                        ).hexdigest(),
                    }
                },
            }
        )
    )
    (training / "comparison.json").write_text(
        json.dumps(
            {
                "test_access": False,
                "development_only": True,
                "configuration_sha256": digest(config),
                "dataset_receipt_sha256": hashlib.sha256(
                    (dataset / "dataset.json").read_bytes()
                ).hexdigest(),
                "manifest_sha256": "b" * 64,
                "models": {
                    "unet": {"run_id": training_run.info.run_id, "inference_artifacts": artifacts}
                },
            }
        )
    )
    result = demonstrate(training, tmp_path / "lifecycle")
    assert [x["action"] for x in result["transitions"]] == ["initial", "promotion", "rollback"]
    assert result["transitions"][0]["model_sha256"] == result["transitions"][-1]["model_sha256"]
    assert all(x["prediction_verified"] for x in result["transitions"])
    assert (
        client.get_model_version_by_alias(result["registered_model"], "research-champion").tags[
            "research_only"
        ]
        == "true"
    )
