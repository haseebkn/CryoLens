"""Execute processing tracking with bound recipes and observable run creation."""

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cryolens.eval.cohort import file_digest
from validation import track_report


def report_fixture(tmp_path: Path) -> dict[str, Any]:
    config = Path("configs/sentinel1-validation-v2.json")
    graph = Path(json.loads(config.read_text())["graph"])
    report = {
        "schema_version": 1,
        "configuration_sha256": file_digest(config),
        "graph_sha256": file_digest(graph),
        "reference_image_id": "sha256:runtime",
        "deployment_gate_open": False,
        "products": [
            {
                "provenance": {"product": "synthetic-test-only"},
                "quality": {"channels": {pol: {"nonpositive_fraction": 0.1} for pol in ("HH", "HV")}},
            }
        ],
    }
    (tmp_path / "report.json").write_text(json.dumps(report))
    (tmp_path / "reference-receipts").mkdir()
    (tmp_path / "reference-receipts/fixture-receipt.json").write_text(
        json.dumps({"image_id": "sha256:runtime", "graph_sha256": report["graph_sha256"]})
    )
    return report


def test_schema_one_v2_execution_tracks_bound_v2_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_fixture(tmp_path)
    starts = []
    run = MagicMock()
    run.run_id = "synthetic-test-run"
    run.client.get_run.return_value.data.tags = {"git_commit": "fixture", "git_dirty": "true"}

    @contextmanager
    def tracking(**kwargs: Any) -> Any:
        starts.append(kwargs)
        yield run

    monkeypatch.setattr(track_report, "tracked_experiment", tracking)
    monkeypatch.setattr(sys, "argv", ["track", "--report-dir", str(tmp_path), "--tracking-uri", "test-only"])
    track_report.main()
    assert len(starts) == 1
    assert starts[0]["experiment_name"] == "cryolens-processing-v2"
    assert starts[0]["configuration"]["recipe_id"] == "v2"
    assert starts[0]["configuration"]["configuration"]["graph"] == "configs/snap/validation-reference-v2.xml"
    saved = json.loads((tmp_path / "tracking_receipt.json").read_text())
    assert saved["recipe_id"] == "v2"
    assert saved["configuration_sha256"] == file_digest(Path("configs/sentinel1-validation-v2.json"))


@pytest.mark.parametrize("conflict", ["unbound", "configuration", "graph", "explicit-v1", "runtime", "receipt-graph"])
def test_conflicting_recipe_never_starts_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, conflict: str
) -> None:
    report = report_fixture(tmp_path)
    argv = ["track", "--report-dir", str(tmp_path), "--tracking-uri", "test-only"]
    if conflict == "unbound":
        report.pop("graph_sha256")
        report.pop("configuration_sha256")
    elif conflict == "configuration":
        report["configuration_sha256"] = "0" * 64
    elif conflict == "graph":
        report["graph_sha256"] = file_digest(Path("configs/snap/validation-reference.xml"))
    elif conflict == "explicit-v1":
        argv.extend(["--config", "configs/sentinel1-validation-v1.json"])
    elif conflict == "runtime":
        report["reference_image_id"] = "sha256:conflict"
    else:
        (tmp_path / "reference-receipts/fixture-receipt.json").write_text(
            json.dumps({"image_id": "sha256:runtime", "graph_sha256": "0" * 64})
        )
    (tmp_path / "report.json").write_text(json.dumps(report))
    start = MagicMock(side_effect=AssertionError("No unbound MLflow run may start"))
    monkeypatch.setattr(track_report, "tracked_experiment", start)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="provenance|conflict"):
        track_report.main()
    start.assert_not_called()
    assert not (tmp_path / "tracking_receipt.json").exists()


def test_graph_bound_historical_recipe_ignores_report_serialization_version() -> None:
    _, _, recipe = track_report.processing_recipe(
        {"schema_version": 2, "graph_sha256": file_digest(Path("configs/snap/validation-reference.xml"))}
    )
    assert recipe == "v1"
