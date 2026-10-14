"""Check decoded measurement and internal overview preservation for SNAP scratch views."""

from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

from cryolens.preprocess.validation import prepare_reference_input


def test_reference_scratch_retains_cog_overviews_and_full_resolution(tmp_path: Path) -> None:
    safe = tmp_path / "source.SAFE"
    (safe / "annotation").mkdir(parents=True)
    (safe / "measurement").mkdir()
    (safe / "manifest.safe").write_text(
        '<manifest><software name="Sentinel-1 IPF" version="004.03"/></manifest>'
    )
    values = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64) + 1
    source = safe / "measurement/s1a-iw-grd-hh-test-001-cog.tiff"
    with rasterio.open(
        source, "w", driver="GTiff", width=64, height=64, count=1, dtype="uint16", tiled=True
    ) as dataset:
        dataset.write(values, 1)
        dataset.build_overviews([2, 4], Resampling.nearest)
    scratch = tmp_path / "scratch.SAFE"
    receipt = prepare_reference_input(safe, scratch)
    with rasterio.open(scratch / "measurement" / source.name) as dataset:
        np.testing.assert_array_equal(dataset.read(1), values)
        assert dataset.overviews(1) == [2, 4]
    assert receipt["measurements"][0]["decoded_pixels_identical"]


def test_reference_scratch_rejects_existing_copy_with_missing_overviews(tmp_path: Path) -> None:
    import pytest

    safe = tmp_path / "source.SAFE"
    (safe / "annotation").mkdir(parents=True)
    (safe / "measurement").mkdir()
    (safe / "manifest.safe").write_text(
        '<manifest><software name="Sentinel-1 IPF" version="004.03"/></manifest>'
    )
    values = np.ones((64, 64), dtype=np.uint16)
    source = safe / "measurement/s1a-iw-grd-hh-test-001-cog.tiff"
    with rasterio.open(
        source, "w", driver="GTiff", width=64, height=64, count=1, dtype="uint16"
    ) as dataset:
        dataset.write(values, 1)
        dataset.build_overviews([2], Resampling.nearest)
    scratch = tmp_path / "scratch.SAFE"
    (scratch / "measurement").mkdir(parents=True)
    with rasterio.open(
        scratch / "measurement" / source.name,
        "w",
        driver="GTiff",
        width=64,
        height=64,
        count=1,
        dtype="uint16",
    ) as dataset:
        dataset.write(values, 1)
    with pytest.raises(ValueError, match="new scratch directory"):
        prepare_reference_input(safe, scratch)


def test_scan_block_configuration_is_validated_before_processing() -> None:
    import json

    import pytest

    from cryolens.preprocess.validation import validate_configuration

    configuration = json.loads(Path("configs/sentinel1-validation-v2.json").read_text())
    configuration["scan_block_rows"] = 32
    validate_configuration(configuration)
    for invalid in [0, -1, True, 3.5]:
        configuration["scan_block_rows"] = invalid
        with pytest.raises(ValueError, match="positive integer"):
            validate_configuration(configuration)
