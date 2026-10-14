"""Behavioral protection, unknown masking and confusion accounting."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from cryolens.segmentation.data import chart_labels, development_records, normalize_db, tie_window
from cryolens.segmentation.metrics import confusion, summarize


def test_protected_partition_rejected_before_opening_sources() -> None:
    with pytest.raises(ValueError, match="development partitions only"):
        development_records({}, "test")


def test_unknown_chart_and_out_of_scope_are_not_background() -> None:
    sic = np.array([[0, 1, 2, 10, 255, -1]])
    eligible = np.array([[True, True, True, False, True, True]])
    np.testing.assert_array_equal(chart_labels(sic, eligible), [[0, 0, 1, 255, 255, 255]])


def test_shared_adapter_neutral_inputs_and_clipping() -> None:
    x = np.empty((1, 2, 2, 2), dtype=np.float32)
    x[:, 0] = -20
    x[:, 1] = -30
    np.testing.assert_array_equal(normalize_db(x), np.zeros_like(x))
    x[:] = 100
    assert np.isfinite(normalize_db(x)).all()


def test_window_tie_grid_preserves_global_endpoints() -> None:
    grid = np.array([[0.0, 10.0], [20.0, 30.0]])
    result = tie_window(grid, (5, 5), 3, 3, 2)
    np.testing.assert_allclose(result, [[22.5, 25.0], [27.5, 30.0]])


def test_confusion_ignores_unknown_and_reports_absent_class() -> None:
    m = confusion(np.array([0, 0, 1, 1]), np.array([0, 1, 1, 255]))
    np.testing.assert_array_equal(m, [[1, 0], [1, 1]])
    result = summarize(m)
    assert result["scored_pixels"] == 3 and result["mean_iou"] == 0.5
    assert summarize(np.array([[4, 0], [0, 0]]))["classes"][1]["recall"] is None


@pytest.mark.parametrize("case", ["changed_array", "test_receipt", "escaped_file"])
def test_registry_rejects_unqualified_cache_before_array_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import json

    from cryolens.eval.cohort import digest, file_digest
    from cryolens.segmentation.lifecycle import validation_example

    root = tmp_path / "cache"
    root.mkdir()
    data = root / "validation.npz"
    data.write_bytes(b"synthetic cache content; never an array")
    config = {"dataset_root": str(root)}
    receipt = {
        "test_access": case == "test_receipt",
        "manifest_sha256": "b" * 64,
        "configuration_sha256": digest(config),
        "partitions": {
            "validation": {
                "file": "../reserved.npz" if case == "escaped_file" else "validation.npz",
                "sha256": file_digest(data),
            }
        },
    }
    receipt_path = root / "dataset.json"
    receipt_path.write_text(json.dumps(receipt))
    comparison = {
        "dataset_receipt_sha256": file_digest(receipt_path),
        "manifest_sha256": "b" * 64,
        "configuration_sha256": digest(config),
    }
    if case == "changed_array":
        data.write_bytes(b"changed bytes")
    opened = []

    def forbidden_array_read(*args: Any, **kwargs: Any) -> None:
        opened.append(True)
        raise AssertionError("Unqualified array was opened")

    monkeypatch.setattr(np, "load", forbidden_array_read)
    with pytest.raises(ValueError):
        validation_example(config, comparison)
    assert opened == []
