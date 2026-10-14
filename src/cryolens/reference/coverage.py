"""Scene coverage with exclusions applied after completed tile assembly."""

from pathlib import Path
from typing import Any

import numpy as np


def uncertain_radius(body: dict[str, Any], protocol_radius: int) -> int:
    approximate = body.get("position_is_approximate", False)
    allowance = body.get("position_uncertainty_pixels")
    if not isinstance(approximate, bool):
        raise ValueError("Position approximation must be explicitly boolean")
    if not approximate:
        if allowance is not None:
            raise ValueError("Pixel uncertainty requires an explicit approximate position")
        return protocol_radius
    if not isinstance(allowance, int) or isinstance(allowance, bool) or allowance < 1:
        raise ValueError("Approximate position needs explicit positive pixel uncertainty")
    return protocol_radius + allowance


def survey_masks(
    root: Path,
    workspace: dict[str, Any],
    tasks: list[dict[str, Any]],
    responses: dict[str, dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    coverage = np.zeros(workspace["source_shape"], dtype=bool)
    eligible = np.zeros_like(coverage)
    excluded = np.zeros_like(coverage)
    height, width = coverage.shape
    for task in tasks:
        if task["mode"] != "survey":
            continue
        row, col = task["origin"]
        with np.load(root / task["grid"], allow_pickle=False) as grid:
            domain = grid["survey_domain"] & grid["core"]
            h, w = domain.shape
            eligible[row : row + h, col : col + w] |= grid["detector_eligible"] & grid["core"]
            body = responses.get(task["task_id"])
            if body is None:
                continue
            radius = uncertain_radius(body, workspace["policy"]["uncertain_buffer_pixels"])
            response = body["response"]
            if response["status"] == "complete":
                coverage[row : row + h, col : col + w] |= domain
            for r0, c0, r1, c1 in response["excluded_regions"]:
                excluded[row + r0 : row + r1, col + c0 : col + c1] = True
            for mark in response["marks"]:
                if mark["label"] == "uncertain":
                    r, c = row + mark["row"], col + mark["col"]
                    excluded[
                        max(0, r - radius) : min(height, r + radius + 1),
                        max(0, c - radius) : min(width, c + radius + 1),
                    ] = True
    return coverage & ~excluded, eligible
