"""Executable evidence aggregation cannot turn a rejected IW into a release."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cryolens.preprocess.quality import QualityPolicy
from validation.finalise_acquisition_report import main


@pytest.mark.parametrize("marine_passed", [True, False])
def test_summary_requires_matching_marine_quality_and_keeps_gate_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marine_passed: bool
) -> None:
    policy = QualityPolicy().model_dump()
    footprint = {
        "type": "Polygon",
        "coordinates": [[[-56, 53], [-54, 53], [-54, 55], [-56, 55], [-56, 53]]],
    }
    rows = []
    for mode, folder, quality_passed, fraction in [
        ("EW", "ew-northern-qualified", True, 0.03),
        ("IW", "iw-northern-reference", False, 0.062),
    ]:
        name = mode + ".SAFE"
        quality = {
            "passed": quality_passed,
            "policy": policy,
            "channels": {"HV": {"nonpositive_fraction": fraction}},
        }
        rows.append(
            {
                "product": name,
                "mode": mode,
                "acquired_utc": "2026-03-25T00:00:00Z",
                "archive_bytes": 1,
                "footprint": footprint,
                "quality": quality,
            }
        )
        out = tmp_path / folder
        out.mkdir()
        (out / "report.json").write_text(
            json.dumps(
                {
                    "scientific_comparison_passed": True,
                    "deployment_gate_open": quality_passed,
                    "validation_source_sha256": {"safe_reader.py": "executed-reader"},
                    "products": [
                        {"product": name, "mode": mode, "windows": [{}] * 9, "quality": quality}
                    ],
                }
            )
        )
    (tmp_path / "report.json").write_text("{}")
    (tmp_path / "continuation").mkdir()
    (tmp_path / "continuation/report.json").write_text(
        json.dumps({"policy": policy, "products": rows, "downloaded_archive_bytes": 2})
    )
    (tmp_path / "ew-northern-qualified/marine-quality.json").write_text(
        json.dumps(
            {
                "passed": marine_passed,
                "policy": policy,
                "product": "EW.SAFE",
                "reader_sha256": "executed-reader",
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["finalise_acquisition_report", "--report-dir", str(tmp_path)])
    if not marine_passed:
        with pytest.raises(ValueError, match="marine quality"):
            main()
        assert not (tmp_path / "current-status.json").exists()
        return
    main()
    summary = json.loads((tmp_path / "current-status.json").read_text())
    assert summary["qualified_modes"] == ["EW"]
    assert summary["deployment_gate_open"] is False
    assert summary["step2_complete"] is False
    assert summary["scientific_release_granted"] is False
    assert len(summary["products"]) == 2
    coverage = json.loads((tmp_path / "current-coverage.geojson").read_text())
    assert len(coverage["features"]) == 2
    assert {f["properties"]["mode"] for f in coverage["features"]} == {"EW", "IW"}
    assert all(f["geometry"] == footprint for f in coverage["features"])
    with pytest.raises(FileExistsError, match="Preserve"):
        main()
