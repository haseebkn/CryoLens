"""Pixel-confusion metrics for chart proxies; no target-accuracy inference."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray


def confusion(predictions: NDArray[Any], labels: NDArray[Any]) -> NDArray[np.int64]:
    mask = labels != 255
    if np.any((predictions[mask] < 0) | (predictions[mask] > 1)):
        raise ValueError("Expected binary class predictions")
    return np.bincount(
        (2 * labels[mask].astype(int) + predictions[mask]).ravel(), minlength=4
    ).reshape(2, 2)


def summarize(matrix: NDArray[Any]) -> dict[str, Any]:
    classes: list[dict[str, Any]] = []
    for k, name in enumerate(["open_water", "ice_affected"]):
        tp = int(matrix[k, k])
        truth = int(matrix[k].sum())
        predicted = int(matrix[:, k].sum())
        union = truth + predicted - tp
        classes.append(
            {
                "label": name,
                "truth_pixels": truth,
                "predicted_pixels": predicted,
                "precision": tp / predicted if predicted else None,
                "recall": tp / truth if truth else None,
                "f1": 2 * tp / (truth + predicted) if truth + predicted else None,
                "iou": tp / union if union else None,
            }
        )
    ious = [item["iou"] for item in classes if item["iou"] is not None]
    return {
        "confusion": matrix.tolist(),
        "scored_pixels": int(matrix.sum()),
        "classes": classes,
        "mean_iou": float(np.mean(ious)) if ious else None,
    }
