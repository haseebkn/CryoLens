"""Executable evidence aggregation cannot turn a rejected IW into a release."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.quality import QualityPolicy
from validation.finalise_acquisition_report import main


def test_replay_published_source_bound_acquisition_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    published = (
        Path(__file__).resolve().parents[2] / "docs/processing-validation/v3/source-bound-approval"
    )
    inputs = [
        "report.json",
        "continuation/report.json",
        "ew-northern-qualified/report.json",
        "ew-northern-qualified/marine-quality.json",
        "iw-northern-reference/report.json",
    ]
    inputs.extend(
        path.relative_to(published).as_posix()
        for path in (published / "continuation/downloads").glob("*.json")
    )
    original_status = (published / "current-status.json").read_bytes()
    for name in inputs:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(published / name, target)
    monkeypatch.setattr(sys, "argv", ["finalise", "--report-dir", str(tmp_path)])
    main()
    status = json.loads((tmp_path / "current-status.json").read_bytes())
    assert status["downloaded_products"] == 10
    assert status["qualified_modes"] == ["EW"]
    assert sum(row["passing_product_checks"] for row in status["products"]) == 1
    iw = [row for row in status["products"] if row["mode"] == "IW"]
    assert len(iw) == 7
    assert all(not row["quality_passed"] and not row["passing_product_checks"] for row in iw)
    for gate in ("step2_complete", "scientific_release_granted", "deployment_gate_open"):
        assert status[gate] is False
    coverage = json.loads((tmp_path / "current-coverage.geojson").read_bytes())
    assert len(coverage["features"]) == 10
    assert (tmp_path / "current-coverage.png").is_file()
    assert (published / "current-status.json").read_bytes() == original_status


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_download_receipt_byte_contract_survives_git_checkout(
    tmp_path: Path, autocrlf: str
) -> None:
    import hashlib
    import subprocess

    from cryolens.preprocess.safe_reader import _open_path

    root = Path(__file__).resolve().parents[2]
    base = Path("docs/processing-validation/v3")
    contracts = {}
    for folder, count in [(base, 9), (base / "continuation", 10)]:
        report = json.loads((root / folder / "report.json").read_bytes())
        products = [p for p in report["products"] if "download_receipt_sha256" in p]
        assert len(products) == count
        for product in products:
            path = folder / "downloads" / (product["product"] + ".json")
            contracts[path] = product["download_receipt_sha256"]
    bound = base / "source-bound-approval"
    search = json.loads((root / bound / "continuation/report.json").read_bytes())
    compared = {
        json.loads((root / bound / folder / "report.json").read_bytes())["products"][0]["product"]
        for folder in ("ew-northern-qualified", "iw-northern-reference")
    }
    for product in search["products"]:
        if product["product"] in compared:
            path = bound / "continuation/downloads" / (product["product"] + ".json")
            contracts[path] = product["download_receipt_sha256"]
    assert len(contracts) == 21
    for name in (".gitattributes", ".gitignore"):
        (tmp_path / name).write_bytes((root / name).read_bytes())

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            [
                "git", "-C", str(tmp_path), "-c", f"core.autocrlf={autocrlf}",
                "-c", "core.longpaths=true", *args,
            ]
        )

    git("init", "--quiet")
    for path, expected in contracts.items():
        content = (root / path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected
        target = Path(_open_path(tmp_path / path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git("add", ".gitattributes", ".gitignore", *(p.as_posix() for p in contracts))
    for path, expected in contracts.items():
        assert hashlib.sha256(git("show", ":" + path.as_posix())).hexdigest() == expected
        target = Path(_open_path(tmp_path / path))
        target.unlink()
        git("checkout-index", "--force", path.as_posix())
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
    for folder in {path.parent for path in contracts}:
        ignored = folder / "raw-data.bin"
        assert git("check-ignore", ignored.as_posix()).decode().strip() == ignored.as_posix()
    assert git("check-ignore", "unrelated/downloads/receipt.json").decode().strip()


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_git_preserves_archived_execution_graph_bytes(tmp_path: Path, autocrlf: str) -> None:
    import hashlib
    import subprocess
    import zipfile

    root = Path(__file__).resolve().parents[2]
    evidence = root / "docs/processing-validation/v3/ew-northern-qualified"
    report = json.loads((evidence / "report.json").read_text())
    graph = "configs/snap/validation-reference-v2.xml"
    with zipfile.ZipFile(evidence / "executed-code.zip") as archive:
        executed = archive.read(graph)
    assert hashlib.sha256(executed).hexdigest() == report["graph_sha256"]
    assert (root / graph).read_bytes() == executed
    (tmp_path / ".gitattributes").write_bytes((root / ".gitattributes").read_bytes())
    target = tmp_path / graph
    target.parent.mkdir(parents=True)
    target.write_bytes(executed)

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "-c", f"core.autocrlf={autocrlf}", *args]
        )

    git("init", "--quiet")
    git("add", ".gitattributes", graph)
    blob = git("show", ":" + graph)
    assert hashlib.sha256(blob).hexdigest() == report["graph_sha256"]
    target.unlink()
    git("checkout-index", "--force", graph)
    assert target.read_bytes() == executed


@pytest.mark.parametrize("autocrlf", ["true", "false"])
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_historical_reports_and_full_receipt_inventory_survive_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, autocrlf: str, version: str
) -> None:
    import hashlib
    import subprocess
    import zipfile
    from contextlib import contextmanager
    from typing import Any
    from unittest.mock import MagicMock

    from validation import track_report

    root = Path(__file__).resolve().parents[2]
    folder = Path("docs/processing-validation") / version
    report = json.loads((root / folder / "report.json").read_bytes())
    tracking = json.loads((root / folder / "tracking_receipt.json").read_bytes())
    config = Path(f"configs/sentinel1-validation-{version}.json")
    graph = Path(json.loads((root / config).read_bytes())["graph"])
    contracts = {
        folder / "report.json": tracking["report_sha256"],
        graph: report["graph_sha256"],
    }
    if version == "v1":
        contracts[config] = report["configuration_sha256"]
    required = {
        window["reference_receipt_sha256"]
        for product in report["products"]
        for window in product["windows"]
    }
    assert len(required) == 27
    for receipt in (root / folder / "reference-receipts").glob("*.json"):
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        if digest in required:
            contracts[receipt.relative_to(root)] = digest
    assert len(contracts) == 29 + (version == "v1")
    (tmp_path / ".gitattributes").write_bytes((root / ".gitattributes").read_bytes())
    for path, expected in contracts.items():
        content = (root / path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected
        assert (
            hashlib.sha256(
                subprocess.check_output(["git", "-C", str(root), "show", ":" + path.as_posix()])
            ).hexdigest()
            == expected
        )
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (tmp_path / config).write_bytes((root / config).read_bytes())

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "-c", f"core.autocrlf={autocrlf}", *args]
        )

    git("init", "--quiet")
    git("add", ".gitattributes", config.as_posix(), *(p.as_posix() for p in contracts))
    for path, expected in contracts.items():
        assert hashlib.sha256(git("show", ":" + path.as_posix())).hexdigest() == expected
        (tmp_path / path).unlink()
        git("checkout-index", "--force", path.as_posix())
        assert hashlib.sha256((tmp_path / path).read_bytes()).hexdigest() == expected
    (tmp_path / config).unlink()
    git("checkout-index", "--force", config.as_posix())
    if version == "v2":
        with zipfile.ZipFile(root / folder / "executed-code.zip") as archive:
            inventory = {
                "src/cryolens/preprocess/" + name: digest
                for name, digest in report["source_sha256"].items()
            }
            inventory["validation/validate_marine_processing.py"] = report["driver_sha256"]
            inventory[graph.as_posix()] = report["graph_sha256"]
            assert set(archive.namelist()) == set(inventory)
            for member, expected in inventory.items():
                assert hashlib.sha256(archive.read(member)).hexdigest() == expected
    starts = []
    run = MagicMock()
    run.run_id = "synthetic-history-check"
    run.client.get_run.return_value.data.tags = {"git_commit": "fixture", "git_dirty": "true"}

    @contextmanager
    def tracking_run(**kwargs: Any) -> Any:
        starts.append(kwargs)
        yield run

    monkeypatch.chdir(tmp_path)
    (tmp_path / "validation").mkdir()
    monkeypatch.setattr(track_report, "tracked_experiment", tracking_run)
    monkeypatch.setattr(
        sys, "argv", ["track", "--report-dir", str(folder), "--tracking-uri", "test-only"]
    )
    track_report.main()
    assert len(starts) == 1
    assert starts[0]["configuration"]["recipe_graph_sha256"] == report["graph_sha256"]
    saved = json.loads((tmp_path / folder / "tracking_receipt.json").read_bytes())
    assert saved["report_sha256"] == tracking["report_sha256"]
    assert saved["recipe_id"] == version


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_short_block_execution_contracts_survive_git_checkout(
    tmp_path: Path, autocrlf: str
) -> None:
    import hashlib
    import subprocess
    import zipfile

    from cryolens.preprocess.validation import execution_artifact_bytes
    from validation.track_report import processing_recipe

    root = Path(__file__).resolve().parents[2]
    folder = Path("docs/processing-validation/v2/short-block-v3")
    report = json.loads((root / folder / "report.json").read_bytes())
    tracking = json.loads((root / folder / "tracking_receipt.json").read_bytes())
    snapshot = folder / "executed-code.zip"
    config = Path("configs/sentinel1-short-block-validation.json")
    graph = Path("configs/snap/validation-reference.xml")
    contracts = {
        folder / "report.json": tracking["report_sha256"],
        snapshot: report["execution_snapshot_sha256"],
        config: report["configuration_sha256"],
        folder / "reference-receipts/short-block-receipt.json": report["products"][0]["provenance"][
            "reference_receipt_sha256"
        ],
        Path("docs/processing-validation/v2/short-block-report.json"): report["products"][0][
            "quality_reused_from_historical_report_sha256"
        ],
        Path("docs/processing-validation/v2/reference-inventory-audit.json"): report[
            "previous_invalidation_sha256"
        ],
        Path("docs/processing-validation/v2/reference-receipts/short-block-receipt.json"): report[
            "previous_reference_receipt_sha256"
        ],
        graph: json.loads((root / "docs/processing-validation/v1/report.json").read_bytes())[
            "graph_sha256"
        ],
    }
    historical_root = Path("docs/processing-validation/v2")
    audit = json.loads((root / historical_root / "reference-inventory-audit.json").read_bytes())
    contracts.update(
        {
            historical_root / name: digest
            for name, digest in audit["short_block_invalidation"][
                "historical_artifacts_sha256"
            ].items()
        }
    )
    (tmp_path / ".gitattributes").write_bytes((root / ".gitattributes").read_bytes())
    for path, expected in contracts.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / path).read_bytes())
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "-c", f"core.autocrlf={autocrlf}", *args]
        )

    git("init", "--quiet")
    git("add", ".gitattributes", *(path.as_posix() for path in contracts))
    for path, expected in contracts.items():
        assert hashlib.sha256(git("show", ":" + path.as_posix())).hexdigest() == expected
        (tmp_path / path).unlink()
        git("checkout-index", "--force", path.as_posix())
        assert hashlib.sha256((tmp_path / path).read_bytes()).hexdigest() == expected
    with zipfile.ZipFile(tmp_path / snapshot) as archive:
        inventory = {
            name if not name.startswith("C:/") else "validation/refresh_short_block.py": digest
            for name, digest in report["execution_source_sha256"].items()
        }
        assert set(archive.namelist()) == set(inventory)
        for name, expected in inventory.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        executed_graph = archive.read(graph.as_posix())
    assert (
        execution_artifact_bytes(
            graph, report["graph_sha256"], tmp_path / snapshot, report["execution_snapshot_sha256"]
        )
        == executed_graph
    )
    selected = processing_recipe(report, config, tmp_path / snapshot)
    assert selected.recipe_id == "short-block-v3"
    assert selected.configuration["window"] == report["products"][0]["window"]
    with pytest.raises(ValueError, match="changed"):
        execution_artifact_bytes(tmp_path / graph, report["graph_sha256"])
    with pytest.raises(ValueError, match="snapshot"):
        execution_artifact_bytes(graph, report["graph_sha256"], tmp_path / snapshot, "0" * 64)


@pytest.mark.parametrize(
    "marine_case",
    [
        "valid",
        "rejected",
        "unbound",
        "measurement_changed",
        "annotation_changed",
        "manifest_changed",
        "download_changed",
        "download_inventory",
        "download_annotation_added",
        "download_missing",
        "processing_reject",
    ],
)
def test_summary_requires_matching_marine_quality_and_keeps_gate_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marine_case: str
) -> None:
    policy = QualityPolicy().model_dump()
    footprint = {
        "type": "Polygon",
        "coordinates": [[[-56, 53], [-54, 53], [-54, 55], [-56, 55], [-56, 53]]],
    }
    provenance = {
        "original_manifest_sha256": "0" * 64,
        "measurements": [
            {"measurement": "hh.tiff", "source_sha256": "1" * 64},
            {"measurement": "hv.tiff", "source_sha256": "2" * 64},
        ],
        "annotation_sha256": {"annotation/hh.xml": "3" * 64},
    }
    source_inventory = {
        "manifest.safe": "0" * 64,
        "measurement/hh.tiff": "1" * 64,
        "measurement/hv.tiff": "2" * 64,
        "annotation/hh.xml": "3" * 64,
    }
    if marine_case == "measurement_changed":
        source_inventory["measurement/hh.tiff"] = "9" * 64
    if marine_case == "annotation_changed":
        source_inventory["annotation/hh.xml"] = "9" * 64
    if marine_case == "manifest_changed":
        source_inventory["manifest.safe"] = "9" * 64
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
        if marine_case == "processing_reject" and mode == "IW":
            quality["channels"] = {}
            quality["reasons"] = ["Missing noise annotation"]
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
                        {
                            "product": name,
                            "mode": mode,
                            "windows": [{}] * 9,
                            "quality": quality,
                            "provenance": provenance,
                        }
                    ],
                }
            )
        )
    (tmp_path / "report.json").write_text("{}")
    (tmp_path / "continuation").mkdir()
    downloads = tmp_path / "continuation/downloads"
    downloads.mkdir()
    for row in rows:
        receipt_path = downloads / (row["product"] + ".json")
        inventory = {
            "manifest.safe": "0" * 64,
            "measurement/hh.tiff": "1" * 64,
            "measurement/hv.tiff": "2" * 64,
            "annotation/hh.xml": "3" * 64,
            "preview/quick-look.png": "4" * 64,
        }
        if row["mode"] == "EW" and marine_case == "download_inventory":
            inventory["measurement/hv.tiff"] = "9" * 64
        if row["mode"] == "EW" and marine_case == "download_annotation_added":
            inventory["annotation/extra.xml"] = "9" * 64
        receipt_path.write_text(
            json.dumps({"catalogue": {"Name": row["product"]}, "safe_files_sha256": inventory})
        )
        row["download_receipt_sha256"] = file_digest(receipt_path)
        if row["mode"] == "EW" and marine_case == "download_changed":
            receipt_path.write_bytes(receipt_path.read_bytes() + b" ")
        if row["mode"] == "EW" and marine_case == "download_missing":
            receipt_path.unlink()
    (tmp_path / "continuation/report.json").write_text(
        json.dumps({"policy": policy, "products": rows, "downloaded_archive_bytes": 2})
    )
    (tmp_path / "ew-northern-qualified/marine-quality.json").write_text(
        json.dumps(
            {
                "schema_version": 1 if marine_case == "unbound" else 2,
                "source_files_sha256": source_inventory,
                "source_inventory_verified_after_scan": True,
                "passed": marine_case != "rejected",
                "policy": policy,
                "product": "EW.SAFE",
                "reader_sha256": "executed-reader",
            }
        )
    )
    monkeypatch.setattr(sys, "argv", ["finalise_acquisition_report", "--report-dir", str(tmp_path)])
    if marine_case not in {"valid", "processing_reject"}:
        with pytest.raises(
            ValueError,
            match="Acquisition" if marine_case.startswith("download") else "marine quality",
        ):
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
    if marine_case == "processing_reject":
        rejected = next(p for p in summary["products"] if p["mode"] == "IW")
        assert rejected["status"] == "processing_reject"
        assert rejected["quality_passed"] is False
        assert rejected["nonpositive_HV_fraction"] is None
        assert rejected["quality_reasons"] == ["Missing noise annotation"]
    coverage = json.loads((tmp_path / "current-coverage.geojson").read_text())
    assert len(coverage["features"]) == 2
    assert {f["properties"]["mode"] for f in coverage["features"]} == {"EW", "IW"}
    assert all(f["geometry"] == footprint for f in coverage["features"])
    with pytest.raises(FileExistsError, match="Preserve"):
        main()


@pytest.mark.parametrize("artifact", ["current-coverage.geojson", "current-coverage.png"])
def test_aggregation_refuses_partial_existing_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact: str
) -> None:
    occupied = tmp_path / artifact
    occupied.write_bytes(b"historical coverage")
    monkeypatch.setattr(sys, "argv", ["finalise", "--report-dir", str(tmp_path)])
    with pytest.raises(FileExistsError, match="Preserve"):
        main()
    assert occupied.read_bytes() == b"historical coverage"
    assert not (tmp_path / "current-status.json").exists()


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_original_audit_byte_contract_survives_git_checkout(tmp_path: Path, autocrlf: str) -> None:
    import hashlib
    import subprocess

    root = Path(__file__).resolve().parents[2]
    folder = Path("docs/audits/pr6-9")
    report = json.loads((root / folder / "report.json").read_bytes())
    baseline = json.loads((root / "docs/processing-validation/v2/report.json").read_bytes())
    contracts = {folder / "report.json": baseline["baseline_report_sha256"]}
    required = {w["reference_receipt_sha256"] for p in report["sentinel1"] for w in p["windows"]}
    assert len(required) == 27
    for path in (root / folder / "reference-receipts").glob("*.json"):
        digest = file_digest(path)
        assert digest in required
        contracts[path.relative_to(root)] = digest
    assert len(contracts) == 28
    (tmp_path / ".gitattributes").write_bytes((root / ".gitattributes").read_bytes())
    (tmp_path / ".gitignore").write_bytes((root / ".gitignore").read_bytes())

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "-c", f"core.autocrlf={autocrlf}", *args]
        )

    git("init", "--quiet")
    for path, expected in contracts.items():
        assert (
            hashlib.sha256(
                subprocess.check_output(["git", "-C", str(root), "show", ":" + path.as_posix()])
            ).hexdigest()
            == expected
        )
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((root / path).read_bytes())
    git("add", ".gitattributes", *(p.as_posix() for p in contracts))
    for path, expected in contracts.items():
        assert hashlib.sha256(git("show", ":" + path.as_posix())).hexdigest() == expected
        (tmp_path / path).unlink()
        git("checkout-index", "--force", path.as_posix())
        assert file_digest(tmp_path / path) == expected
    published_receipt = (
        tmp_path / "docs/processing-validation/v3/continuation/downloads/fixture.json"
    )
    published_receipt.parent.mkdir(parents=True)
    published_receipt.write_text("{}")
    git("add", published_receipt.relative_to(tmp_path).as_posix())
    assert git("show", ":" + published_receipt.relative_to(tmp_path).as_posix()).strip() == b"{}"
    assert git("check-ignore", "downloads/private.json").strip() == b"downloads/private.json"
    raster = "docs/processing-validation/v3/continuation/downloads/raw.tiff"
    assert git("check-ignore", raster).decode().strip() == raster


def test_published_processing_artifacts_preserve_their_recorded_byte_contract() -> None:
    import hashlib
    import subprocess

    from cryolens.eval.cohort import file_digest
    from validation.publish_validation_run import verify_execution_sources

    root = Path(__file__).resolve().parents[2] / "docs/processing-validation/v3"
    status = json.loads((root / "current-status.json").read_text())
    assert file_digest(root / "report.json") == status["baseline_report_sha256"]
    assert file_digest(root / "continuation/report.json") == status["continuation_report_sha256"]
    for folder in ["ew-northern-qualified", "iw-northern-reference", "iw-noise-edge"]:
        out = root / folder
        report = json.loads((out / "report.json").read_text())
        tracking = json.loads((out / "tracking_receipt.json").read_text())
        assert file_digest(out / "report.json") == tracking["report_sha256"]
        git_blob = subprocess.check_output(
            [
                "git",
                "-C",
                str(root.parents[2]),
                "show",
                "HEAD:docs/processing-validation/v3/" + folder + "/report.json",
            ]
        )
        assert hashlib.sha256(git_blob).hexdigest() == tracking["report_sha256"]
        assert file_digest(out / "configuration.json") == report["configuration_sha256"]
        verify_execution_sources(report, out)
        receipts = {file_digest(p) for p in (out / "reference-receipts").glob("*.json")}
        required = {
            w["reference_receipt_sha256"]
            for product in report["products"]
            for w in product["windows"]
        }
        assert required <= receipts


def test_new_marine_receipt_matches_original_processing_inputs() -> None:
    from cryolens.eval.cohort import file_digest
    from cryolens.preprocess.provenance import original_inventory
    from validation.publish_validation_run import verify_execution_sources

    root = (
        Path(__file__).resolve().parents[2] / "docs/processing-validation/v3/source-bound-approval"
    )
    for folder in ["ew-northern-qualified", "iw-northern-reference"]:
        out = root / folder
        report = json.loads((out / "report.json").read_text())
        tracking = json.loads((out / "tracking_receipt.json").read_text())
        assert file_digest(out / "report.json") == tracking["report_sha256"]
        verify_execution_sources(report, out)
    ew = json.loads((root / "ew-northern-qualified/report.json").read_text())
    marine = json.loads((root / "ew-northern-qualified/marine-quality.json").read_text())
    assert marine["schema_version"] == 2
    assert marine["source_inventory_verified_after_scan"] is True
    assert marine["source_files_sha256"] == original_inventory(ew["products"][0]["provenance"])
    assert marine["reader_sha256"] == ew["validation_source_sha256"]["safe_reader.py"]
    assert marine["passed"] is True
    assert json.loads((root / "current-status.json").read_text())["step2_complete"] is False
