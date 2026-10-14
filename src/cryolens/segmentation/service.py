"""Read-only CPU inference for a research chart-proxy model, with a fixed contract."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from typing import Any

import numpy as np
import torch
from fastapi import FastAPI, HTTPException, Request, Response


def create_app(model_path: Path, expected_sha256: str) -> FastAPI:
    """Load only an explicitly checksum-qualified locally supplied TorchScript."""
    actual = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError("Inference artifact checksum mismatch")
    model = torch.jit.load(str(model_path), map_location="cpu").eval()
    torch.set_num_threads(2)
    app = FastAPI(title="CryoLens research sea-ice segmentation")

    @app.get("/metadata")
    def metadata() -> dict[str, Any]:
        return {
            "model_sha256": actual,
            "task": "chart_proxy_segmentation",
            "research_only": True,
            "input": {
                "dtype": "float32",
                "shape": [1, 2, 128, 128],
                "channel_order": ["HH", "HV"],
                "units": "physical_sigma0_db",
            },
            "output": {
                "dtype": "float32",
                "shape": [1, 2, 128, 128],
                "classes": ["open_water", "ice_affected"],
            },
            "limitations": "Not target detection or navigation; chart agreement and development only; apply original valid/land/unknown masks separately",
        }

    @app.post("/segment")
    async def segment(request: Request) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024**2:
                raise HTTPException(413, "Input is too large")
        try:
            header = io.BytesIO(body)
            version = np.lib.format.read_magic(header)
            if version == (1, 0):
                array_shape, _, dtype = np.lib.format.read_array_header_1_0(header)
            elif version == (2, 0):
                array_shape, _, dtype = np.lib.format.read_array_header_2_0(header)
            else:
                raise ValueError("Unsupported NPY version")
            if array_shape != (1, 2, 128, 128) or dtype != np.float32:
                raise ValueError("Unexpected NPY header; rejected before allocation")
            data = np.load(io.BytesIO(body), allow_pickle=False)
            if (
                not isinstance(data, np.ndarray)
                or data.shape != (1, 2, 128, 128)
                or data.dtype != np.float32
            ):
                raise ValueError("Expected float32 [1,2,128,128] .npy")
            if not np.isfinite(data).all():
                raise ValueError("Nonfinite inputs must be masked before inference")
        except Exception as exc:
            raise HTTPException(
                422, "Expected finite float32 HH/HV physical-dB .npy [1,2,128,128]"
            ) from exc
        with torch.no_grad():
            prediction = model(torch.from_numpy(data)).numpy().astype(np.float32)
        if (
            prediction.shape != (1, 2, 128, 128)
            or not np.isfinite(prediction).all()
            or not np.allclose(prediction.sum(1), 1, atol=1e-5)
        ):
            raise HTTPException(500, "Invalid model output")
        stream = io.BytesIO()
        np.save(stream, prediction, allow_pickle=False)
        return Response(
            stream.getvalue(),
            media_type="application/octet-stream",
            headers={"X-Model-Sha256": actual, "X-Research-Only": "true"},
        )

    return app


def factory() -> FastAPI:
    return create_app(Path(os.environ["MODEL_PATH"]), os.environ["MODEL_SHA256"])
