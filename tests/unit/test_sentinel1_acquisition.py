"""Execute test-group protection and SAFE manifest integrity checks."""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from validation.acquire_sentinel1 import guard, verify_manifest

NAME = "S1A_EW_GRDM_1SDH_20260314T211636_20260314T211740_063629_07FF73_69C8_COG.SAFE"


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
