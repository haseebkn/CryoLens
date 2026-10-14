"""Exercise legacy publication with immutable occupied output namespaces."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from validation import publish_report, run_nersc


@pytest.mark.parametrize("occupied", ["empty", "historic"])
def test_legacy_publisher_refuses_occupied_output_before_reading_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, occupied: str
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    originals = {}
    if occupied == "historic":
        for name in ("report.json", "tracking_receipt.json", "reference-receipts/original.json"):
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[name] = ("historical " + name).encode()
            path.write_bytes(originals[name])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish",
            "--report",
            "missing-report",
            "--iw-receipt",
            "missing-receipt",
            "--nersc-report",
            "missing-nersc",
            "--output-dir",
            str(output),
        ],
    )
    with pytest.raises(FileExistsError, match="Preserve published evidence"):
        publish_report.main()
    assert {
        p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()
    } == originals


@pytest.mark.parametrize("missing", [None, "quality", "receipt"])
def test_legacy_publication_validates_inputs_before_creating_new_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str | None
) -> None:
    root = Path(__file__).resolve().parents[2]
    historical = root / "docs/processing-validation/v1"
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    for path in [historical / "report.json", *historical.glob("*-full-quality.json")]:
        shutil.copyfile(path, inputs / path.name)
    shutil.copytree(historical / "reference-receipts", inputs / "reference")
    if missing == "quality":
        next(inputs.glob("*-full-quality.json")).unlink()
    if missing == "receipt":
        next((inputs / "reference").glob("*.json")).unlink()
    output = tmp_path / "published"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish",
            "--report",
            str(inputs / "report.json"),
            "--iw-receipt",
            str(historical / "iw-integrity.json"),
            "--nersc-report",
            str(historical / "nersc-signed-power.json"),
            "--output-dir",
            str(output),
        ],
    )
    if missing:
        with pytest.raises(ValueError, match="retained full|reference receipt inventory"):
            publish_report.main()
        assert not output.exists()
    else:
        publish_report.main()
        assert (output / "report.json").read_bytes() == (inputs / "report.json").read_bytes()
        assert json.loads((output / "report.json").read_bytes())["deployment_gate_open"] is False
        for source in (inputs / "reference").glob("*.json"):
            assert (output / "reference-receipts" / source.name).read_bytes() == source.read_bytes()


def test_nersc_driver_refuses_existing_outputs_before_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "nersc"
    output.mkdir()
    snapshot = output / "nersc-hv.npy"
    snapshot.write_bytes(b"historical signed power")
    monkeypatch.setattr(sys, "argv", ["nersc", "S1A_fixture.SAFE", str(output)])
    with pytest.raises(FileExistsError):
        run_nersc.main()
    assert snapshot.read_bytes() == b"historical signed power"
