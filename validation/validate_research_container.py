"""Execute qualified research model versions in Docker and verify real rollback."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np
import requests
from mlflow import MlflowClient

from cryolens.eval.cohort import file_digest


def validate(registry_path: Path, output: Path, image: str) -> dict[str, Any]:
    """Local-only containers with one read-only model bind; preserve all evidence."""
    registry = json.loads(registry_path.read_text())
    if not registry["research_only"] or registry["test_access"]:
        raise ValueError("Expected a development-only research registry")
    output.mkdir(parents=True, exist_ok=False)
    client = MlflowClient(tracking_uri=registry["tracking_uri"])
    image_info = json.loads(
        subprocess.check_output(["docker", "image", "inspect", image], encoding="utf-8")
    )[0]
    image_id = image_info["Id"]
    transitions = []
    versions = {v["version"]: v for v in registry["versions"]}
    request_file = registry_path.parent / "request.npy"
    request_bytes = request_file.read_bytes()
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "research_only": True,
        "test_access": False,
        "registry_sha256": file_digest(registry_path),
        "driver_sha256": file_digest(Path(__file__)),
        "image_id": image_id,
        "request_sha256": file_digest(request_file),
        "numerical_tolerance_absolute": 1e-5,
        "transitions": transitions,
        "complete": False,
    }
    for index, step in enumerate(registry["transitions"]):
        selected = versions[step["version"]]
        path = Path(selected["artifact"])
        if file_digest(path) != selected["model_sha256"]:
            raise ValueError("Qualified model changed")
        client.set_registered_model_alias(
            registry["registered_model"], "research-champion", selected["version"]
        )
        resolved = client.get_model_version_by_alias(
            registry["registered_model"], "research-champion"
        )
        if (
            str(resolved.version) != str(selected["version"])
            or resolved.tags["model_sha256"] != selected["model_sha256"]
        ):
            raise ValueError("Registry alias did not bind selected model")
        name = "cryolens-seaice-" + output.name.lower().replace("_", "-") + "-" + str(index)
        cmd = [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1:8030:8080",
            "--mount",
            f"type=bind,source={path.resolve()},target=/models/model.pt,readonly",
            "--env",
            "MODEL_SHA256=" + selected["model_sha256"],
            image_id,
        ]
        container = subprocess.check_output(cmd, encoding="utf-8").strip()
        try:
            deadline = time.monotonic() + 45
            while True:
                try:
                    metadata = requests.get("http://127.0.0.1:8030/metadata", timeout=2)
                    metadata.raise_for_status()
                    break
                except requests.RequestException:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Container did not become ready") from None
                    time.sleep(0.5)
            observed = metadata.json()
            if (
                observed["model_sha256"] != selected["model_sha256"]
                or not observed["research_only"]
            ):
                raise ValueError("Wrong serving artifact")
            response = requests.post(
                "http://127.0.0.1:8030/segment",
                data=request_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=30,
            )
            response.raise_for_status()
            prediction = np.load(io.BytesIO(response.content), allow_pickle=False)
            expected = np.load(
                registry_path.parent / ("version-" + str(resolved.version) + "-expected.npy"),
                allow_pickle=False,
            )
            max_error = float(np.max(np.abs(prediction - expected)))
            if (
                response.headers["X-Model-Sha256"] != selected["model_sha256"]
                or not np.isfinite(prediction).all()
                or not np.allclose(prediction, expected, rtol=0, atol=1e-5)
            ):
                raise ValueError(f"Container inference mismatch: {max_error}")
            np.save(output / (step["action"] + "-prediction.npy"), prediction, allow_pickle=False)
            malformed = requests.post("http://127.0.0.1:8030/segment", data=b"not-npy", timeout=10)
            if malformed.status_code != 422:
                raise ValueError("Malformed request accepted")
            oversized = io.BytesIO()
            np.lib.format.write_array_header_1_0(
                oversized,
                {"descr": "<f4", "fortran_order": False, "shape": (1000000000, 2, 128, 128)},
            )
            header_response = requests.post(
                "http://127.0.0.1:8030/segment", data=oversized.getvalue(), timeout=10
            )
            if header_response.status_code != 422:
                raise ValueError("Oversized NPY header accepted")
            transitions.append(
                {
                    "action": step["action"],
                    "version": str(resolved.version),
                    "model_sha256": selected["model_sha256"],
                    "container_id": container,
                    "image_id": image_id,
                    "max_absolute_prediction_error": max_error,
                    "shape": list(prediction.shape),
                    "malformed_input_status": malformed.status_code,
                    "oversized_header_status": header_response.status_code,
                    "research_only": observed["research_only"],
                    "passed": True,
                }
            )
            (output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(step["action"], "version", resolved.version, "max error", max_error, flush=True)
        finally:
            logs = subprocess.run(
                ["docker", "logs", container], capture_output=True, encoding="utf-8"
            )
            (output / (step["action"] + "-container.log")).write_text(logs.stdout + logs.stderr)
            subprocess.run(["docker", "stop", container], check=True, capture_output=True)
    if transitions[0]["model_sha256"] != transitions[-1]["model_sha256"]:
        raise ValueError("Rollback did not restore original model")
    receipt["complete"] = True
    (output / "verification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="cryolens-seaice-research:development-v1")
    args = parser.parse_args()
    validate(args.registry, args.output, args.image)
