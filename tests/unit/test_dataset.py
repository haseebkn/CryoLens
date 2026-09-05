"""Regression tests for candidate-chip extraction and split leakage."""

from pathlib import Path

import numpy as np
import pytest

from cryolens.detect.dataset import ChipRecord, DatasetBuilder


def test_odd_chip_size_is_preserved(tmp_path: Path) -> None:
    result = DatasetBuilder(tmp_path, chip_size=5).extract_chip({"hv": np.ones((20, 20))}, 10, 10)
    assert result is not None
    assert result[0]["hv"].shape == (5, 5)


def test_scene_groups_cannot_leak_between_train_and_validation(tmp_path: Path) -> None:
    builder = DatasetBuilder(tmp_path)
    records = [
        ChipRecord(
            tmp_path / "images" / f"{scene}_{i}.tif",
            tmp_path / "labels" / f"{scene}_{i}.txt",
            0,
            scene,
            str(i),
            100,
            100,
        )
        for scene in ("scene_a", "scene_b")
        for i in range(3)
    ]
    builder.write_data_yaml(records)
    train = (tmp_path / "train.txt").read_text().splitlines()
    val = (tmp_path / "val.txt").read_text().splitlines()
    assert len(train) == len(val) == 3
    assert set(train).isdisjoint(val)
    assert all("scene_a" in item for item in train)
    assert all("scene_b" in item for item in val)
    with pytest.raises(ValueError, match="two independently"):
        builder.write_data_yaml(records[:3])
