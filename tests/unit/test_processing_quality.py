"""Processing regressions: signed noise, real interfaces, rejection receipts and warps."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from cryolens.preprocess.__main__ import main
from cryolens.preprocess.python_chain import PurePythonSARProcessor
from cryolens.preprocess.quality import (
    ProcessingQualityError,
    assess_safe_product,
    require_channels,
    require_detection_ready_cog,
)
from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.stack import BAND_NAMES, COGStackBuilder
from cryolens.preprocess.validation import (
    compare_power,
    ipf_version,
    measure_registration,
    require_development_product,
)
from tests.unit.test_safe_reader import safe_product as safe_product


def channels(safe: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    reader = SAFEProductReader(safe)
    return reader.read_sigma0("HH"), reader.read_sigma0("HV")


def test_window_matches_full_calibration_and_annotation(safe_product: Path) -> None:
    reader = SAFEProductReader(safe_product)
    full = reader.read_sigma0("HV")
    window = reader.read_sigma0("HV", window=(7, 13, 11, 17))
    for name in ("sigma0_linear", "latitude", "longitude", "incidence_angle_deg"):
        np.testing.assert_allclose(window[name], full[name][7:18, 13:30], rtol=1e-6)
    with pytest.raises(ValueError, match="Window"):
        reader.read_sigma0("HV", window=(39, 59, 2, 2))


def test_azimuth_noise_extrapolates_to_declared_block_edge(safe_product: Path) -> None:
    path = next((safe_product / "annotation/calibration").glob("noise-*hv*.xml"))
    path.write_text(
        path.read_text().replace(
            "</noise>",
            "<noiseAzimuthVectorList><noiseAzimuthVector><firstAzimuthLine>0</firstAzimuthLine><lastAzimuthLine>39</lastAzimuthLine><firstRangeSample>0</firstRangeSample><lastRangeSample>59</lastRangeSample><line>0 36</line><noiseAzimuthLut>1 4</noiseAzimuthLut></noiseAzimuthVector></noiseAzimuthVectorList></noise>",
        )
    )
    power = SAFEProductReader(safe_product).read_sigma0("HV")["sigma0_linear"]
    assert power[39, 0] == pytest.approx(100 - 4 * 4.25 / 10000, abs=1e-5)


def test_short_azimuth_block_uses_nearest_qualified_swath_range_vector(safe_product: Path) -> None:
    from cryolens.preprocess.safe_reader import _noise_power_grid

    path = next((safe_product / "annotation/calibration").glob("noise-*hv*.xml"))
    vectors = "".join(
        f"<noiseAzimuthVector><swath>EW1</swath><firstAzimuthLine>{lo}</firstAzimuthLine>"
        f"<lastAzimuthLine>{hi}</lastAzimuthLine><firstRangeSample>0</firstRangeSample>"
        f"<lastRangeSample>59</lastRangeSample><line>{lo} {hi}</line>"
        "<noiseAzimuthLut>1 1</noiseAzimuthLut></noiseAzimuthVector>"
        for lo, hi in [(0, 9), (10, 19), (20, 39)]
    )
    path.write_text(
        path.read_text().replace(
            "</noise>", f"<noiseAzimuthVectorList>{vectors}</noiseAzimuthVectorList></noise>"
        )
    )
    power = _noise_power_grid(path, (40, 60), full_shape=(40, 60))
    np.testing.assert_allclose(power, 4)
    # The fallback is annotation-derived, and must not invent a profile when
    # the nearest vectors contain no valid noise within this swath's range.
    path.write_text(path.read_text().replace("<noiseRangeLut>4.0 4.0", "<noiseRangeLut>0.0 0.0"))
    with pytest.raises(ValueError, match="qualified"):
        _noise_power_grid(path, (40, 60), full_shape=(40, 60))


def test_unannotated_noise_padding_is_allowed_only_for_actual_nodata(safe_product: Path) -> None:
    reader = SAFEProductReader(safe_product)
    measurement = reader._find_for_polarisation("HV")[0]
    with rasterio.open(measurement, "r+") as raster:
        data = raster.read(1)
        data[:, :10] = data[:, 50:] = 0
        raster.write(data, 1)
    noise = next((safe_product / "annotation/calibration").glob("noise-*hv*.xml"))
    noise.write_text(
        noise.read_text().replace(
            "</noise>",
            "<noiseAzimuthVectorList><noiseAzimuthVector><firstAzimuthLine>0</firstAzimuthLine><lastAzimuthLine>39</lastAzimuthLine><firstRangeSample>10</firstRangeSample><lastRangeSample>49</lastRangeSample><line>0 39</line><noiseAzimuthLut>1 1</noiseAzimuthLut></noiseAzimuthVector></noiseAzimuthVectorList></noise>",
        )
    )
    result = reader.read_sigma0("HV")
    assert np.isnan(result["sigma0_linear"][:, :10]).all()
    np.testing.assert_allclose(
        result["sigma0_linear"][:, 10:50],
        (data[:, 10:50].astype(float) ** 2 - 4) / 10000,
        rtol=1e-6,
    )
    with rasterio.open(measurement, "r+") as raster:
        data[:, 0] = 1000
        raster.write(data, 1)
    with pytest.raises(ValueError, match="cover"):
        reader.read_sigma0("HV")


def test_iw_uses_actual_annotation_not_nominal_ew_ramp(safe_product: Path) -> None:
    iw = safe_product.with_name("S1A_IW_GRDH_1SDH_TEST.SAFE")
    safe_product.rename(iw)
    reader = SAFEProductReader(iw)
    result = reader.read_sigma0("HH", window=(10, 10, 10, 10))
    assert reader.instrument_mode == "IW"
    assert result["incidence_angle_deg"][0, 0] == pytest.approx(20 + 25 * 10 / 59, abs=1e-5)


def test_uncorrected_channel_is_never_detection_ready(safe_product: Path) -> None:
    raw = SAFEProductReader(safe_product).read_sigma0("HV", remove_thermal_noise=False)
    assert raw["usable_for_cfar"] is False
    hh, _ = channels(safe_product)
    with pytest.raises(ProcessingQualityError, match="correction not"):
        require_channels(hh, raw)


def test_quality_rejects_nonpositive_incidence_and_misalignment(
    safe_product: Path, tmp_path: Path
) -> None:
    hh, hv = channels(safe_product)
    hv["sigma0_linear"][:10] = -1
    report = tmp_path / "quality.json"
    with pytest.raises(ProcessingQualityError, match="non-positive"):
        require_channels(hh, hv, report)
    assert json.loads(report.read_text())["passed"] is False
    hh, hv = channels(safe_product)
    hv["incidence_angle_deg"][0, 0] = np.nan
    with pytest.raises(ProcessingQualityError, match="incidence"):
        require_channels(hh, hv)
    hh, hv = channels(safe_product)
    hv["latitude"] += 0.001
    with pytest.raises(ProcessingQualityError, match="mismatch"):
        require_channels(hh, hv)


def test_empty_radiometry_fails_and_stays_nodata(safe_product: Path) -> None:
    reader = SAFEProductReader(safe_product)
    measurement = reader._find_for_polarisation("HV")[0]
    with rasterio.open(measurement, "r+") as dst:
        dst.write(np.zeros(dst.shape, dtype=np.uint16), 1)
    result = reader.read_sigma0("HV")
    assert np.isnan(result["sigma0_linear"]).all()
    assert result["usable_for_cfar"] is False
    hh = reader.read_sigma0("HH")
    with pytest.raises(ProcessingQualityError, match="valid observations"):
        require_channels(hh, result)


def test_bounded_full_scan_counts_pixels_and_reports_missing_input(
    safe_product: Path, tmp_path: Path
) -> None:
    (safe_product / "manifest.safe").write_text("<manifest/>")
    report = assess_safe_product(
        SAFEProductReader(safe_product), tmp_path / "report.json", block_rows=7
    )
    assert report["passed"] is True
    assert report["channels"]["HV"]["finite_pixels"] == 40 * 60
    assert len(report["stripes"]) == 6
    next((safe_product / "annotation/calibration").glob("noise-*hv*.xml")).unlink()
    with pytest.raises(ProcessingQualityError, match="noise annotation"):
        assess_safe_product(SAFEProductReader(safe_product), tmp_path / "report.json")
    assert json.loads((tmp_path / "report.json").read_text())["passed"] is False


def test_preprocessing_cli_records_failure_without_export(
    safe_product: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (safe_product / "manifest.safe").write_text("<manifest/>")
    path = next((safe_product / "annotation/calibration").glob("noise-*hv*.xml"))
    path.write_text(path.read_text().replace("4.0", "10000000.0"))
    output = tmp_path / "processed"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preprocess",
            "--scene",
            str(safe_product),
            "--engine",
            "python",
            "--output-dir",
            str(output),
        ],
    )
    assert main() == 1
    assert not list(output.rglob("*.tif"))
    receipt = json.loads(next(output.rglob("processing-quality.json")).read_text())
    assert receipt["passed"] is False
    assert any("HV" in reason for reason in receipt["reasons"])


def test_export_and_detection_cannot_bypass_fresh_quality(tmp_path: Path) -> None:
    bands = {
        name: np.full((32, 32), 35 if name == "incidence_angle" else -20, dtype=np.float32)
        for name in BAND_NAMES
    }
    bands["ratio_hh_hv"][:] = 0
    builder = COGStackBuilder(tmp_path)
    transform = from_origin(100000, 500000, 40, 40)
    with pytest.raises(ValueError, match="receipt"):
        builder.build_and_export_cog("S1C_IW_GRDH_1SDH_REAL", bands, transform)
    path = builder.build_and_export_cog(
        "diagnostic",
        bands,
        transform,
        provenance={"source_kind": "sentinel1_safe", "processing_quality": "rejected"},
    )
    with rasterio.open(path) as src, pytest.raises(ValueError, match="quality"):
        require_detection_ready_cog(src)


def test_weak_positive_signal_is_not_floored_at_minus_50_db() -> None:
    shape = (12, 12)
    latitude = np.tile(np.linspace(48.01, 48, 12)[:, None], (1, 12))
    longitude = np.tile(np.linspace(-53.01, -53, 12), (12, 1))
    result = PurePythonSARProcessor().process_calibrated_arrays(
        np.full(shape, 1e-4), np.full(shape, 1e-7), np.full(shape, 35), latitude, longitude
    )
    values = result["bands"]["sigma0_hv_db"]
    np.testing.assert_allclose(values[values != result["nodata"]], -70, atol=1e-4)
    assert result["resampling_domain"] == "linear_power"


def test_ipf_is_selected_by_name_not_cogifier(safe_product: Path) -> None:
    (safe_product / "manifest.safe").write_text(
        '<manifest><software name="Sentinel-1 COGifier" version="001.00"/><software name="Sentinel-1 IPF" version="004.03"/></manifest>'
    )
    assert ipf_version(safe_product) == "004.03"


def test_no_reference_overlap_cannot_pass() -> None:
    assert compare_power(np.full((64, 64), np.nan), np.ones((64, 64)))["passed"] is False
    assert compare_power(np.ones((64, 64)), np.ones((64, 64)))["passed"] is True
    assert compare_power(np.ones((64, 64)), np.full((64, 64), 2))["passed"] is False


def test_registration_finds_displaced_spatial_pattern() -> None:
    rng = np.random.default_rng(17)
    image = rng.normal(size=(96, 96))
    valid = np.ones(image.shape, dtype=bool)
    same = measure_registration(image, image, valid, 40)
    shifted = measure_registration(image, np.roll(image, (3, -2), axis=(0, 1)), valid, 40)
    assert same["passed"] is True
    assert shifted["relative_displacement_m"] == pytest.approx(np.hypot(3, 2) * 40)
    assert shifted["passed"] is False
    assert (
        measure_registration(np.ones_like(image), np.ones_like(image), valid, 40)["passed"] is False
    )


def test_frozen_holdout_cannot_become_processing_development() -> None:
    safe = Path("S1C_EW_GRDM_1SDH_20260912T090816_20260912T090908_009412_012B82_FF9F_COG.SAFE")
    with pytest.raises(ValueError, match="Frozen test"):
        require_development_product(safe, Path("docs/evaluation/v1/manifest.json"))


@pytest.mark.parametrize(
    "change",
    [
        {"products": []},
        {"sampling_fractions": []},
        {"sampling_fractions": [[float("nan"), 0.5]]},
        {"sampling_fractions": [[0.5, 0.5], [0.5, 0.5]]},
        {"sampling_fractions": [[1.1, 0.5]]},
        {"window_size_px": 0},
    ],
)
def test_validation_cli_rejects_empty_or_invalid_comparison_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    from cryolens.preprocess import validation

    config = {
        "products": [{"name": "fixture", "path": "fixture.SAFE"}],
        "sampling_fractions": [[0.5, 0.5]],
        "window_size_px": 512,
        **change,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validation",
            "--config",
            str(path),
            "--output-dir",
            str(tmp_path / "output"),
            "--scratch-dir",
            str(tmp_path / "scratch"),
        ],
    )
    with pytest.raises(ValueError, match="Validation"):
        validation.main()
    assert not (tmp_path / "output").exists()


@pytest.fixture
def reference_cache_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, tuple[int, int, int, int]]:
    from cryolens.preprocess import validation

    scratch = tmp_path / "S1A_EW_GRDM_fixture.SAFE"
    scratch.mkdir()
    (scratch / "manifest.safe").write_text("<manifest/>")
    (scratch / "annotation/calibration").mkdir(parents=True)
    (scratch / "measurement").mkdir()
    for pol in ("hh", "hv"):
        (scratch / "measurement" / f"s1a-ew-grd-{pol}-fixture.tiff").write_bytes(b"measurement")
        (scratch / "annotation" / f"s1a-ew-grd-{pol}-fixture.xml").write_text("<product/>")
        for kind in ("calibration", "noise"):
            (
                scratch / "annotation/calibration" / f"{kind}-s1a-ew-grd-{pol}-fixture.xml"
            ).write_text(f"<{kind}/>")
    graph = tmp_path / "graph.xml"
    graph.write_text("<graph/>")
    monkeypatch.setattr(validation.subprocess, "check_output", lambda *a, **kw: "sha256:runtime")
    prefix = tmp_path / "example"
    Path(str(prefix) + "_geo.tif").write_bytes(b"geo")
    for stage in ("raw", "den"):
        Path(str(prefix) + f"_{stage}.dim").write_text("<dim/>")
        folder = Path(str(prefix) + f"_{stage}.data")
        folder.mkdir()
        bands = (
            ("Sigma0_HH", "Sigma0_HV")
            if stage == "raw"
            else ("sigma0_hh", "sigma0_hv", "latitude_band", "longitude_band", "incidence")
        )
        for band in bands:
            (folder / f"{band}.img").write_bytes(stage.encode())
            (folder / f"{band}.hdr").write_text("ENVI\nsamples = 512\n")
    window = (0, 0, 512, 512)
    validation.write_reference_receipt(scratch, graph, prefix, window, "runtime")
    return scratch, graph, prefix, window


def test_reference_cache_consumes_verified_archived_graph(
    reference_cache_fixture: tuple[Path, Path, Path, tuple[int, int, int, int]],
    tmp_path: Path,
) -> None:
    import zipfile

    from cryolens.eval.cohort import file_digest
    from cryolens.preprocess import validation

    scratch, graph, prefix, window = reference_cache_fixture
    root = Path(__file__).resolve().parents[2]
    evidence = root / "docs/processing-validation/v2/short-block-v3"
    report = json.loads((evidence / "report.json").read_bytes())
    snapshot = evidence / "executed-code.zip"
    member = Path("configs/snap/validation-reference.xml")
    with zipfile.ZipFile(snapshot) as archive:
        graph.write_bytes(archive.read(member.as_posix()))
    Path(str(prefix) + "-receipt.json").unlink()
    validation.write_reference_receipt(scratch, graph, prefix, window, "runtime")
    graph.write_bytes((root / member).read_bytes())
    before = Path(str(prefix) + "-receipt.json").read_bytes()
    with pytest.raises(ValueError, match="changed"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    validation.verify_reference_receipt(
        scratch,
        member,
        prefix,
        window,
        "runtime",
        execution_snapshot=snapshot,
        execution_snapshot_sha256=report["execution_snapshot_sha256"],
    )
    assert Path(str(prefix) + "-receipt.json").read_bytes() == before
    corrupted = tmp_path / "changed.zip"
    corrupted.write_bytes(snapshot.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="snapshot"):
        validation.verify_reference_receipt(
            scratch,
            member,
            prefix,
            window,
            "runtime",
            execution_snapshot=corrupted,
            execution_snapshot_sha256=report["execution_snapshot_sha256"],
        )
    assert file_digest(snapshot) == report["execution_snapshot_sha256"]
    Path(str(prefix) + "_geo.tif").write_bytes(b"changed")
    with pytest.raises(ValueError, match="output.*changed"):
        validation.verify_reference_receipt(
            scratch,
            member,
            prefix,
            window,
            "runtime",
            execution_snapshot=snapshot,
            execution_snapshot_sha256=report["execution_snapshot_sha256"],
        )


def test_reference_cache_rejects_recipe_and_pixel_changes(
    reference_cache_fixture: tuple[Path, Path, Path, tuple[int, int, int, int]],
) -> None:
    from cryolens.preprocess import validation

    scratch, graph, prefix, window = reference_cache_fixture
    validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    header = Path(str(prefix) + "_den.data/sigma0_hv.hdr")
    header.write_text("ENVI\nsamples = 256\n")
    with pytest.raises(ValueError, match="changed"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    header.write_text("ENVI\nsamples = 512\n")
    receipt_path = Path(str(prefix) + "-receipt.json")
    receipt = json.loads(receipt_path.read_text())
    receipt_path.write_text(json.dumps({**receipt, "outputs_sha256": {}}))
    with pytest.raises(ValueError, match="incomplete"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    receipt_path.write_text(json.dumps(receipt))
    graph.write_text("<different-graph/>")
    with pytest.raises(ValueError, match="changed"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    graph.write_text("<graph/>")
    (scratch / "manifest.safe").write_text("<changed/>")
    with pytest.raises(ValueError, match="changed"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    (scratch / "manifest.safe").write_text("<manifest/>")
    Path(str(prefix) + "_geo.tif").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")


@pytest.mark.parametrize(
    "changed",
    [
        "annotation/s1a-ew-grd-hh-fixture.xml",
        "annotation/calibration/calibration-s1a-ew-grd-hh-fixture.xml",
        "annotation/calibration/noise-s1a-ew-grd-hv-fixture.xml",
        "measurement/s1a-ew-grd-hv-fixture.tiff",
        "incomplete-receipt",
    ],
)
def test_reference_cache_binds_linked_directories_and_rejects_incomplete_receipts(
    reference_cache_fixture: tuple[Path, Path, Path, tuple[int, int, int, int]],
    changed: str,
) -> None:
    from cryolens.preprocess import validation

    scratch, graph, prefix, window = reference_cache_fixture
    for directory in ("annotation", "measurement"):
        original = scratch / directory
        target = scratch.parent / (directory + "-target")
        original.rename(target)
        try:
            original.symlink_to(target, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(original), str(target)],
                check=True,
                capture_output=True,
            )
    validation.write_reference_receipt(scratch, graph, prefix, window, "runtime")
    validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    receipt_path = Path(str(prefix) + "-receipt.json")
    receipt = json.loads(receipt_path.read_text())
    if changed == "incomplete-receipt":
        receipt["scratch_files_sha256"] = {
            "manifest.safe": receipt["scratch_files_sha256"]["manifest.safe"]
        }
        receipt_path.write_text(json.dumps(receipt))
    else:
        (scratch / changed).write_bytes(b"changed target")
    preserved = receipt_path.read_bytes()
    with pytest.raises(ValueError, match="incomplete|changed"):
        validation.verify_reference_receipt(scratch, graph, prefix, window, "runtime")
    assert receipt_path.read_bytes() == preserved


@pytest.mark.parametrize(
    "missing",
    [
        "annotation/s1a-ew-grd-hh-fixture.xml",
        "annotation/calibration/calibration-s1a-ew-grd-hh-fixture.xml",
        "annotation/calibration/noise-s1a-ew-grd-hv-fixture.xml",
        "measurement/s1a-ew-grd-hv-fixture.tiff",
    ],
)
def test_reference_identity_rejects_missing_consumed_components(
    reference_cache_fixture: tuple[Path, Path, Path, tuple[int, int, int, int]],
    missing: str,
) -> None:
    from cryolens.preprocess import validation

    scratch, graph, _, window = reference_cache_fixture
    (scratch / missing).unlink()
    with pytest.raises((ValueError, FileNotFoundError), match="[Ii]ncomplete"):
        validation.reference_identity(scratch, graph, window, "runtime")


def test_reference_identity_hashes_inputs_beyond_windows_path_limit(
    reference_cache_fixture: tuple[Path, Path, Path, tuple[int, int, int, int]],
) -> None:
    from cryolens.preprocess import validation
    from cryolens.preprocess.safe_reader import _open_path

    scratch, graph, _, window = reference_cache_fixture
    long_scratch = scratch.parent / ("S1A_EW_GRDM_" + "x" * 100 + ".SAFE")
    shutil.copytree(scratch, _open_path(long_scratch))
    original = validation.reference_identity(scratch, graph, window, "runtime")
    copied = validation.reference_identity(long_scratch, graph, window, "runtime")
    assert copied == original


def test_short_block_keeps_measured_zero_edge_knots(tmp_path: Path) -> None:
    """A measured positive profile with zero border knots is not missing noise."""
    from cryolens.preprocess.safe_reader import _noise_power_grid

    path = tmp_path / "noise.xml"
    ranges = "".join(
        f"<noiseRangeVector><line>{line}</line><pixel>0 3 5</pixel>"
        "<noiseRangeLut>4 4 0</noiseRangeLut></noiseRangeVector>"
        for line in (0, 9)
    )
    azimuth = "".join(
        f"<noiseAzimuthVector><swath>IW3</swath><firstAzimuthLine>{lo}</firstAzimuthLine>"
        f"<lastAzimuthLine>{hi}</lastAzimuthLine><firstRangeSample>0</firstRangeSample>"
        f"<lastRangeSample>5</lastRangeSample><line>{lo} {hi}</line>"
        "<noiseAzimuthLut>1 1</noiseAzimuthLut></noiseAzimuthVector>"
        for lo, hi in ((0, 2), (3, 5), (6, 9))
    )
    path.write_text(f"<noise>{ranges}{azimuth}</noise>")
    full = _noise_power_grid(path, (10, 6), full_shape=(10, 6))
    np.testing.assert_allclose(full, np.tile([4, 4, 4, 4, 2, 0], (10, 1)))
    window = _noise_power_grid(path, (2, 3), offset=(4, 3), full_shape=(10, 6))
    np.testing.assert_allclose(window, full[4:6, 3:6])
    original = path.read_text()
    path.write_text(original.replace("4 4 0", "4 4 -1"))
    with pytest.raises(ValueError, match="qualified|nonnegative"):
        _noise_power_grid(path, (10, 6), full_shape=(10, 6))
    path.write_text(original.replace("4 4 0", "0 0 0"))
    with pytest.raises(ValueError, match="qualified"):
        _noise_power_grid(path, (10, 6), full_shape=(10, 6))


@pytest.mark.parametrize(
    "suffix", [".log", "-receipt.json", "_geo.tif", "_den.data", "-execution.lock"]
)
def test_reference_run_rejects_occupied_prefix_before_docker(
    tmp_path: Path, mocker: Any, suffix: str
) -> None:
    from cryolens.preprocess.validation import run_reference

    prefix = tmp_path / "reference"
    existing = Path(str(prefix) + suffix)
    if suffix.endswith(".data"):
        existing.mkdir()
        (existing / "band.img").write_bytes(b"historical output")
    else:
        existing.write_bytes(b"historical evidence")
    docker = mocker.patch("cryolens.preprocess.validation.subprocess.run")
    with pytest.raises(FileExistsError, match="occupied"):
        run_reference(tmp_path / "safe", tmp_path / "graph.xml", prefix, (0, 0, 10, 10), "runtime")
    docker.assert_not_called()
    if existing.is_file():
        assert existing.read_bytes() == b"historical evidence"
    else:
        assert (existing / "band.img").read_bytes() == b"historical output"


def test_direct_receipt_write_is_idempotent_but_never_replaces_evidence(
    reference_cache_fixture: tuple[Path, Path, Path, tuple[int, int, int, int]],
) -> None:
    from cryolens.preprocess.validation import write_reference_receipt

    scratch, graph, prefix, window = reference_cache_fixture
    path = Path(str(prefix) + "-receipt.json")
    original = path.read_bytes()
    write_reference_receipt(scratch, graph, prefix, window, "runtime")
    assert path.read_bytes() == original
    Path(str(prefix) + "_geo.tif").write_bytes(b"changed")
    with pytest.raises(FileExistsError, match="already exists"):
        write_reference_receipt(scratch, graph, prefix, window, "runtime")
    assert path.read_bytes() == original
