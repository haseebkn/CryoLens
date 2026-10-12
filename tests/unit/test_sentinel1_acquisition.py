"""Execute test-group protection and SAFE manifest integrity checks."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from validation.acquire_sentinel1 import guard, verify_manifest

NAME = "S1A_EW_GRDM_1SDH_20260314T211636_20260314T211740_063629_07FF73_69C8_COG.SAFE"


@pytest.fixture
def cached_acquisition(tmp_path: Path) -> dict[str, Any]:
    from cryolens.eval.cohort import file_digest
    from cryolens.preprocess.provenance import safe_file_inventory
    from cryolens.preprocess.quality import QualityPolicy
    from cryolens.preprocess.safe_reader import _open_path

    root = Path(_open_path(tmp_path / "acquisition"))
    product = root / "raw" / NAME.removesuffix(".SAFE")
    product.mkdir(parents=True)
    safe = safe_fixture(product)
    safe = safe.rename(product / NAME)
    (safe / "measurement/s1a-ew-grd-hv-fixture.tiff").write_bytes(b"fixture HV")
    (safe / "annotation/calibration").mkdir(parents=True)
    (safe / "annotation/calibration/noise-s1a-ew-grd-hv-fixture.xml").write_text("<noise/>")
    archive = product / (NAME + ".zip")
    import zipfile

    with zipfile.ZipFile(archive, "w") as zipped:
        for path in safe.rglob("*"):
            if path.is_file():
                zipped.write(path, path.relative_to(product).as_posix())
    footprint = {
        "type": "Polygon",
        "coordinates": [[[-56, 53], [-54, 53], [-54, 55], [-56, 55], [-56, 53]]],
    }
    catalogue = {
        "Id": "fixture-catalogue-id",
        "Name": NAME,
        "ContentLength": archive.stat().st_size,
        "GeoFootprint": footprint,
        "Checksum": [{"Algorithm": "MD5", "Value": hashlib.md5(archive.read_bytes()).hexdigest()}],
    }
    inventory = safe_file_inventory(safe)
    receipt = {
        "catalogue": catalogue,
        "safe_files_sha256": inventory,
        "archive_sha256": file_digest(archive),
        "zip_crc_passed": True,
        "manifest_checks": verify_manifest(safe),
    }
    (product / "download-receipt.json").write_text(json.dumps(receipt))
    quality = root / "quality" / (NAME + ".json")
    quality.parent.mkdir()
    quality.write_text(
        json.dumps(
            {
                "product": NAME,
                "scope": "full_measurement_grid",
                "policy": QualityPolicy().model_dump(),
                "passed": True,
                "channels": {"HV": {"nonpositive_fraction": 0.02}},
                "reasons": [],
            }
        )
    )
    quality.with_suffix(".binding.json").write_text(
        json.dumps(
            {
                "report_sha256": file_digest(quality),
                "inputs_and_code": {
                    "safe_files_sha256": inventory,
                    "policy": QualityPolicy().model_dump(),
                    "safe_reader_sha256": file_digest(
                        Path("src/cryolens/preprocess/safe_reader.py")
                    ),
                    "quality_code_sha256": file_digest(Path("src/cryolens/preprocess/quality.py")),
                    "scan_block_rows": 32,
                },
            }
        )
    )
    plan = tmp_path / "data/processed/acquisition-plan-fixture.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        json.dumps(
            {
                "frozen_manifest": "docs/evaluation/v1/manifest.json",
                "selected": [
                    {
                        "name": NAME,
                        "id": catalogue["Id"],
                        "date": "2026-03-14T21:16:36Z",
                        "mode": "EW",
                        "bytes": archive.stat().st_size,
                        "catalogue": catalogue,
                        "aoi_fraction": 1.0,
                    }
                ],
            }
        )
    )
    return {"root": root, "safe": safe, "quality": quality, "plan": plan, "catalogue": catalogue}


def mutate_safe(safe: Path, mutation: str) -> None:
    if mutation == "measurement_added":
        (safe / "measurement/s1a-ew-grd-hv-duplicate.tiff").write_bytes(b"duplicate HV")
    elif mutation == "noise_added":
        (safe / "annotation/calibration/noise-s1a-ew-grd-hv-duplicate.xml").write_text("<noise/>")
    elif mutation == "other_file_added":
        (safe / "extra-component.txt").write_text("additional source component")
    elif mutation == "removed":
        (safe / "measurement/hh.tiff").unlink()
    elif mutation == "changed":
        (safe / "measurement/hh.tiff").write_bytes(b"changed source")


@pytest.mark.parametrize(
    "mutation",
    ["unchanged", "measurement_added", "noise_added", "other_file_added", "removed", "changed"],
)
def test_cached_acquisition_checks_complete_live_safe_inventory(
    cached_acquisition: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from validation import acquire_sentinel1

    safe = cached_acquisition["safe"]
    quality = cached_acquisition["quality"]
    before = quality.read_bytes()
    mutate_safe(safe, mutation)
    monkeypatch.setattr(acquire_sentinel1, "load_manifest", lambda path: {"scenes": []})
    monkeypatch.setattr(acquire_sentinel1, "Settings", lambda **kwargs: SimpleNamespace(cdse=None))
    monkeypatch.setattr(acquire_sentinel1, "CDSEClient", lambda **kwargs: None)
    calls = []
    original = acquire_sentinel1.load_quality_cache

    def load_cache(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
        calls.append(path)
        return original(path, expected)

    monkeypatch.setattr(acquire_sentinel1, "load_quality_cache", load_cache)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acquire",
            "--plan",
            str(cached_acquisition["plan"]),
            "--root",
            str(cached_acquisition["root"]),
            "--env-file",
            "unused-fixture.env",
        ],
    )
    if mutation == "unchanged":
        acquire_sentinel1.main()
        assert calls == [quality]
    else:
        with pytest.raises(ValueError, match="source inventory changed"):
            acquire_sentinel1.main()
        assert calls == []
    assert quality.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    ["unchanged", "measurement_added", "noise_added", "other_file_added", "removed", "changed"],
)
def test_acquisition_publication_checks_complete_live_safe_inventory(
    cached_acquisition: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from shapely.geometry import shape
    from validation import publish_acquisition_search

    mutate_safe(cached_acquisition["safe"], mutation)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        publish_acquisition_search.httpx,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: cached_acquisition["catalogue"]
        ),
    )
    monkeypatch.setattr(
        publish_acquisition_search.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="fixture-docker"),
    )
    monkeypatch.setattr(
        publish_acquisition_search,
        "load_aoi",
        lambda: shape(cached_acquisition["catalogue"]["GeoFootprint"]),
    )
    for path in (tmp_path / "docs/evaluation/v1/manifest.json", tmp_path / "configs/aoi.geojson"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    output = tmp_path / "published"
    monkeypatch.setattr(
        sys, "argv", ["publish", "--root", str(cached_acquisition["root"]), "--output", str(output)]
    )
    if mutation == "unchanged":
        publish_acquisition_search.main()
        published = json.loads((output / "report.json").read_text())
        assert published["products"][0]["status"] == "radiometric_pass"
        assert published["deployment_gate_open"] is False
    else:
        with pytest.raises(ValueError, match="source inventory changed"):
            publish_acquisition_search.main()
        assert not output.exists()


def test_new_product_is_excluded_when_near_frozen_test() -> None:
    record = {"name": NAME, "date": "2026-03-14T21:16:36Z"}
    manifest = {
        "scenes": [
            {
                "partition": "test",
                "acquisition_id": "unrelated",
                "acquired_utc": "2026-03-18T00:00:00Z",
            }
        ]
    }
    with pytest.raises(ValueError, match="frozen test group"):
        guard(record, manifest)
    manifest["scenes"][0]["acquired_utc"] = "2026-03-25T00:00:00Z"
    guard(record, manifest)


def safe_fixture(tmp_path: Path, href: str = "./measurement/hh.tiff") -> Path:
    safe = tmp_path / "product.SAFE"
    (safe / "measurement").mkdir(parents=True)
    content = b"actual fixture bytes"
    (safe / "measurement/hh.tiff").write_bytes(content)
    digest = hashlib.md5(content).hexdigest()
    (safe / "manifest.safe").write_text(
        f'<manifest><dataObject><byteStream size="{len(content)}"><fileLocation href="{href}"/><checksum checksumName="MD5">{digest}</checksum></byteStream></dataObject></manifest>'
    )
    return safe


def test_safe_checksum_checks_actual_bytes(tmp_path: Path) -> None:
    safe = safe_fixture(tmp_path)
    assert verify_manifest(safe) == [
        {"member": "measurement/hh.tiff", "algorithm": "md5", "matched": True}
    ]
    (safe / "measurement/hh.tiff").write_bytes(
        b"X" + (safe / "measurement/hh.tiff").read_bytes()[1:]
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_manifest(safe)


def test_manifest_cannot_reference_file_outside_safe(tmp_path: Path) -> None:
    safe = safe_fixture(tmp_path, "../outside.tiff")
    (tmp_path / "outside.tiff").write_bytes(b"actual fixture bytes")
    with pytest.raises(ValueError, match="absent or unsafe"):
        verify_manifest(safe)


def test_manifest_requires_verified_measurement(tmp_path: Path) -> None:
    safe = tmp_path / "empty.SAFE"
    safe.mkdir()
    (safe / "manifest.safe").write_text("<manifest/>")
    with pytest.raises(ValueError, match="no verified measurement"):
        verify_manifest(safe)


def test_unbound_quality_cache_cannot_be_reused(tmp_path: Path) -> None:
    import json

    from validation.acquire_sentinel1 import load_quality_cache

    quality = tmp_path / "quality.json"
    quality.write_text(json.dumps({"passed": True}))
    with pytest.raises(ValueError, match="lacks execution binding"):
        load_quality_cache(quality, {})


def test_bound_quality_cache_requires_same_input_code_and_strict_policy(tmp_path: Path) -> None:
    import json

    from cryolens.eval.cohort import file_digest
    from cryolens.preprocess.quality import QualityPolicy
    from validation.acquire_sentinel1 import load_quality_cache

    quality = tmp_path / "quality.json"
    result: dict[str, Any] = {
        "scope": "full_measurement_grid",
        "policy": QualityPolicy().model_dump(),
        "passed": False,
    }
    quality.write_text(json.dumps(result))
    expected = {"source": "actual-test-fixture-hash", "reader": "reader-version"}
    quality.with_suffix(".binding.json").write_text(
        json.dumps({"inputs_and_code": expected, "report_sha256": file_digest(quality)})
    )
    assert load_quality_cache(quality, expected)["passed"] is False
    with pytest.raises(ValueError, match="inputs, code or report changed"):
        load_quality_cache(quality, {**expected, "reader": "changed"})
    result["policy"]["max_nonpositive_fraction"] = 0.15
    quality.write_text(json.dumps(result))
    quality.with_suffix(".binding.json").write_text(
        json.dumps({"inputs_and_code": expected, "report_sha256": file_digest(quality)})
    )
    with pytest.raises(ValueError, match="policy differs"):
        load_quality_cache(quality, expected)


def test_processing_publication_requires_actual_execution_sources(tmp_path: Path) -> None:
    import hashlib
    import json
    import zipfile

    from validation.publish_validation_run import verify_execution_sources

    names = [
        "validation.py",
        "quality.py",
        "safe_reader.py",
        "python_chain.py",
        "marine_geocoding.py",
        "stack.py",
    ]
    sources = {"src/cryolens/preprocess/" + name: (name + " executed").encode() for name in names}
    inventory = {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}
    report = {
        "validation_source_sha256": {
            name: inventory["src/cryolens/preprocess/" + name] for name in names
        }
    }
    (tmp_path / "source-sha256.json").write_text(json.dumps(inventory))
    with zipfile.ZipFile(tmp_path / "executed-code.zip", "w") as archive:
        for name, content in sources.items():
            archive.writestr(name, content)
    verify_execution_sources(report, tmp_path)
    report["validation_source_sha256"]["safe_reader.py"] = "0" * 64
    with pytest.raises(ValueError, match="actual execution"):
        verify_execution_sources(report, tmp_path)
    report["validation_source_sha256"].pop("stack.py")
    with pytest.raises(ValueError, match="complete"):
        verify_execution_sources(report, tmp_path)
