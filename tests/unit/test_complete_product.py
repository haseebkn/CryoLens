"""Execute bounded complete-product IO and test scientific failure behavior."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.windows import Window

from cryolens.preprocess import product
from cryolens.preprocess.quality import ProcessingQualityError, require_detection_ready_cog
from cryolens.preprocess.safe_reader import SAFEProductReader
from tests.unit.test_safe_reader import safe_product as safe_product


class AffineFixtureMapper(product.MarineGeocoder):
    """Declared analytic sampling fixture; never used for satellite evidence."""

    full_shape = (40, 60)

    def __init__(self, annotation: Path) -> None:
        self.annotation = annotation

    def coordinates(
        self, lon: np.ndarray, lat: np.ndarray, window: tuple[int, int, int, int]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(lon, lat)
        row = np.round((500000 - y) / 40 - 0.5, 8)
        col = np.round((x - 100000) / 40 - 0.5, 8)
        valid = (row >= 0) & (col >= 0) & (row <= 39) & (col <= 59)
        return row, col, valid

    def provenance(self) -> dict[str, str]:
        return {"geolocation_method": "marine_range_doppler", "height_datum": "test_fixture"}


@pytest.fixture
def executable_product(safe_product: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (safe_product / "manifest.safe").write_text("<manifest/>")
    monkeypatch.setattr(product, "MarineGeocoder", AffineFixtureMapper)
    monkeypatch.setattr(
        product, "product_grid", lambda reader: (from_origin(100000, 500000, 40, 40), 60, 40)
    )
    return safe_product


def test_complete_export_retains_full_scan_and_common_nodata(
    executable_product: Path, tmp_path: Path
) -> None:
    reader = SAFEProductReader(executable_product)
    measurement = reader._find_for_polarisation("HV")[0]
    with rasterio.open(measurement, "r+") as src:
        data = src.read(1)
        data[5, 5] = 0
        src.write(data, 1)
    output = product.process_safe_product(
        executable_product, tmp_path / "out", block_size=16, scan_rows=7
    )
    root = output.parent
    scan = json.loads((root / "source-quality.json").read_text())
    assert scan["scope"] == "full_measurement_grid" and len(scan["stripes"]) == 6
    assert scan["channels"]["HV"]["finite_pixels"] == 2399
    receipt = json.loads((root / "processing-receipt.json").read_text())
    assert receipt["output_quality_passed"] is True
    assert receipt["reference_comparison_completed"] is False
    assert receipt["fresh_detection_allowed"] is False
    assert receipt["traversal"]["destination_pixels"] == 2400
    with rasterio.open(output) as src:
        values = src.read()
        valid = values[0] != product.NODATA
        assert not valid[5, 5]
        assert np.all((values != product.NODATA) == valid)
        np.testing.assert_allclose(values[2, valid], values[0, valid] - values[1, valid], atol=1e-6)
        with pytest.raises(ValueError, match="blocked"):
            require_detection_ready_cog(src)
    assert not (root / "corrected-interim.tif").exists()


def test_tile_partition_does_not_change_corrected_output(
    executable_product: Path, tmp_path: Path
) -> None:
    one = product.process_safe_product(executable_product, tmp_path / "one", block_size=16)
    two = product.process_safe_product(executable_product, tmp_path / "two", block_size=32)
    with rasterio.open(one) as a, rasterio.open(two) as b:
        np.testing.assert_array_equal(a.read(), b.read())


def test_occupied_output_preserves_every_record(executable_product: Path, tmp_path: Path) -> None:
    output = product.process_safe_product(executable_product, tmp_path / "one", block_size=16)
    before = {p.name: p.read_bytes() for p in output.parent.iterdir()}
    with pytest.raises(FileExistsError):
        product.process_safe_product(executable_product, tmp_path / "one", block_size=16)
    assert before == {p.name: p.read_bytes() for p in output.parent.iterdir()}


def test_strict_source_rejection_precedes_output(executable_product: Path, tmp_path: Path) -> None:
    noise = next((executable_product / "annotation/calibration").glob("noise-*hv*.xml"))
    noise.write_text(noise.read_text().replace("4.0", "10000000.0"))
    with pytest.raises(ProcessingQualityError, match="non-positive"):
        product.process_safe_product(executable_product, tmp_path / "out", block_size=16)
    root = tmp_path / "out" / executable_product.stem
    assert not list(root.glob("*.tif"))
    assert json.loads((root / "source-quality.json").read_text())["passed"] is False
    assert json.loads((root / "processing-failure.json").read_text())["passed"] is False


def test_linear_sampling_and_memory_bound(executable_product: Path) -> None:
    reader = SAFEProductReader(executable_product)
    mapper = AffineFixtureMapper(Path("fixture"))
    tile, counts = product.corrected_tile(
        reader, mapper, from_origin(100000, 500000, 40, 40), Window(8, 8, 16, 16)
    )
    # Actual calibrated bright pixel differs from background; no log-domain average.
    assert tile[0, 2, 12] > tile[0, 0, 0]
    assert counts["valid_pixels"] == 256
    with pytest.raises(ValueError, match="memory bound"):
        product.corrected_tile(
            reader, mapper, from_origin(100000, 500000, 40, 40), Window(8, 8, 16, 16), 10
        )


def test_changed_source_during_processing_fails_closed(
    executable_product: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = product.safe_source_inventory
    calls = 0

    def changed(reader: SAFEProductReader) -> dict[str, str]:
        nonlocal calls
        calls += 1
        result = original(reader)
        if calls > 1:
            result["manifest.safe"] = "0" * 64
        return result

    monkeypatch.setattr(product, "safe_source_inventory", changed)
    with pytest.raises(ValueError, match="inputs changed"):
        product.process_safe_product(executable_product, tmp_path / "out", block_size=16)
    assert not list((tmp_path / "out").rglob("*_4band_EPSG3978.tif"))


def test_output_inspection_rejects_ratio_and_nodata_tampering(
    executable_product: Path, tmp_path: Path
) -> None:
    output = product.process_safe_product(executable_product, tmp_path / "out", block_size=16)
    # Updating a COG also invalidates its layout; semantic rejection must survive it.
    with rasterio.open(output, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as src:
        ratio = src.read(3)
        ratio[0, 0] += 1
        src.write(ratio, 3)
    report = product.inspect_corrected_output(output)
    assert report["passed"] is False
    assert report["inconsistent_ratio_pixels"] == 1


def test_big_tiff_repackaging_keeps_every_corrected_pixel(
    executable_product: Path, tmp_path: Path
) -> None:
    original = product.process_safe_product(
        executable_product, tmp_path / "original", block_size=16
    )
    replacement = product.repackage_complete_output(original, tmp_path / "replacement")
    with replacement.open("rb") as stream:
        assert stream.read(4) == b"II+\x00"  # Emitted BigTIFF byte contract (version43).
    assert product.verify_pixel_identity(original, replacement) == 2400
    receipt = json.loads((replacement.parent / "packaging-receipt.json").read_text())
    assert receipt["all_corrected_pixels_identical"] is True
    assert product.inspect_corrected_output(replacement)["overview_quality"]["passed"] is True
    with pytest.raises(FileExistsError):
        product.repackage_complete_output(original, replacement.parent)


def test_installed_package_source_capture_needs_no_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site-packages/cryolens"
    module = package / "preprocess/product.py"
    module.parent.mkdir(parents=True)
    module.write_bytes(b"fixture package bytes")
    configuration = package / "resources/configs/example.json"
    configuration.parent.mkdir(parents=True)
    configuration.write_text("{}")
    monkeypatch.setattr(product, "__file__", str(module))
    execution, payloads = product.execution_context()
    assert execution["context"] == "installed_package"
    assert execution["git_commit"] is None
    assert payloads["src/cryolens/preprocess/product.py"] == module.read_bytes()
    assert payloads["configs/example.json"] == configuration.read_bytes()
