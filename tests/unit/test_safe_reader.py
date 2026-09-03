"""Unit tests for the Sentinel-1 SAFE product reader.

A miniature but structurally faithful SAFE product is synthesised on disk so the
calibration path can be exercised without credentials or a multi-gigabyte
download. The XML element names and nesting mirror the ESA Sentinel-1 Product
Specification, so a change that breaks real products breaks these tests too.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from cryolens.preprocess.safe_reader import (
    CalibrationLUT,
    SAFEProductReader,
    _parse_calibration,
    _parse_noise,
    _parse_product_annotation,
    _polarisation_of,
)

N_LINES, N_SAMPLES = 40, 60
SIGMA_CONSTANT = 100.0
NOISE_CONSTANT = 4.0


def _calibration_xml(sigma: float = SIGMA_CONSTANT) -> str:
    """Two calibration vectors spanning the raster with a constant sigmaNought."""
    pixels = " ".join(str(p) for p in (0, N_SAMPLES - 1))
    values = " ".join(f"{sigma:.1f}" for _ in range(2))
    vectors = "".join(
        f"<calibrationVector><line>{line}</line>"
        f"<pixel>{pixels}</pixel><sigmaNought>{values}</sigmaNought>"
        f"</calibrationVector>"
        for line in (0, N_LINES - 1)
    )
    return f"<calibration><calibrationVectorList>{vectors}</calibrationVectorList></calibration>"


def _noise_xml(noise: float = NOISE_CONSTANT) -> str:
    """Two noise range vectors with a constant noise power in DN squared."""
    pixels = " ".join(str(p) for p in (0, N_SAMPLES - 1))
    values = " ".join(f"{noise:.1f}" for _ in range(2))
    vectors = "".join(
        f"<noiseRangeVector><line>{line}</line>"
        f"<pixel>{pixels}</pixel><noiseRangeLut>{values}</noiseRangeLut>"
        f"</noiseRangeVector>"
        for line in (0, N_LINES - 1)
    )
    return f"<noise><noiseRangeVectorList>{vectors}</noiseRangeVectorList></noise>"


def _annotation_xml() -> str:
    """A product annotation with a 2x2 geolocation grid."""
    points = []
    for line in (0, N_LINES - 1):
        for pixel in (0, N_SAMPLES - 1):
            inc = 20.0 + 25.0 * (pixel / (N_SAMPLES - 1))
            lat = 47.0 + 2.0 * (line / (N_LINES - 1))
            lon = -53.0 + 3.0 * (pixel / (N_SAMPLES - 1))
            points.append(
                f"<geolocationGridPoint><line>{line}</line><pixel>{pixel}</pixel>"
                f"<incidenceAngle>{inc}</incidenceAngle>"
                f"<latitude>{lat}</latitude><longitude>{lon}</longitude>"
                f"</geolocationGridPoint>"
            )
    return (
        "<product><imageAnnotation><imageInformation>"
        f"<numberOfLines>{N_LINES}</numberOfLines>"
        f"<numberOfSamples>{N_SAMPLES}</numberOfSamples>"
        "</imageInformation></imageAnnotation>"
        f"<geolocationGrid><geolocationGridPointList>{''.join(points)}"
        "</geolocationGridPointList></geolocationGrid></product>"
    )


@pytest.fixture
def safe_product(tmp_path: Path) -> Path:
    """Create a minimal dual-pol SAFE product with a known DN field."""
    import rasterio

    safe = tmp_path / "S1A_EW_GRDM_1SDH_TEST.SAFE"
    (safe / "measurement").mkdir(parents=True)
    (safe / "annotation" / "calibration").mkdir(parents=True)

    for pol, index in (("hh", "001"), ("hv", "002")):
        stem = f"s1a-ew-grd-{pol}-20200101t000000-20200101t000100-{index}"

        dn = np.full((N_LINES, N_SAMPLES), 1000.0, dtype=np.float32)
        dn[10, 20] = 5000.0  # a bright point target
        with rasterio.open(
            safe / "measurement" / f"{stem}.tiff",
            "w",
            driver="GTiff",
            height=N_LINES,
            width=N_SAMPLES,
            count=1,
            dtype="uint16",
        ) as dst:
            dst.write(dn.astype(np.uint16), 1)

        (safe / "annotation" / f"{stem}.xml").write_text(_annotation_xml(), encoding="utf-8")
        (safe / "annotation" / "calibration" / f"calibration-{stem}.xml").write_text(
            _calibration_xml(), encoding="utf-8"
        )
        (safe / "annotation" / "calibration" / f"noise-{stem}.xml").write_text(
            _noise_xml(), encoding="utf-8"
        )

    return safe


class TestPolarisationParsing:
    """Polarisation is taken from the SAFE filename convention."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("s1a-ew-grd-hh-20200101t000000-001.tiff", "HH"),
            ("s1a-ew-grd-hv-20200101t000000-002.tiff", "HV"),
            ("s1b-iw-grd-vv-20200101t000000-001.tiff", "VV"),
            ("some-other-file.xml", None),
        ],
    )
    def test_extracts_polarisation(self, name: str, expected: str | None) -> None:
        assert _polarisation_of(Path(name)) == expected


class TestCalibrationLUT:
    """LUT interpolation must expand the coarse grid without distorting values."""

    def test_constant_lut_stays_constant(self) -> None:
        lut = CalibrationLUT(
            lines=np.array([0, 0, 9, 9]),
            pixels=np.array([0, 9, 0, 9]),
            values=np.array([50.0, 50.0, 50.0, 50.0]),
        )
        out = lut.interpolate((10, 10))
        assert out.shape == (10, 10)
        assert out == pytest.approx(np.full((10, 10), 50.0), abs=1e-4)

    def test_range_ramp_is_linear(self) -> None:
        lut = CalibrationLUT(
            lines=np.array([0, 0, 4, 4]),
            pixels=np.array([0, 4, 0, 4]),
            values=np.array([10.0, 50.0, 10.0, 50.0]),
        )
        out = lut.interpolate((5, 5))
        assert out[0, 0] == pytest.approx(10.0, abs=1e-4)
        assert out[0, -1] == pytest.approx(50.0, abs=1e-4)
        assert out[0, 2] == pytest.approx(30.0, abs=1e-3)


class TestAnnotationParsing:
    """Annotation parsing must recover dimensions and the geolocation grid."""

    def test_parses_calibration(self, safe_product: Path) -> None:
        path = next((safe_product / "annotation" / "calibration").glob("calibration-*hh*.xml"))
        lut, _, _ = _parse_calibration(path)
        assert lut.values.size == 4
        assert lut.values == pytest.approx(np.full(4, SIGMA_CONSTANT))

    def test_parses_noise(self, safe_product: Path) -> None:
        path = next((safe_product / "annotation" / "calibration").glob("noise-*hh*.xml"))
        lut = _parse_noise(path)
        assert lut is not None
        assert lut.values == pytest.approx(np.full(4, NOISE_CONSTANT))

    def test_parses_product_annotation(self, safe_product: Path) -> None:
        path = next((safe_product / "annotation").glob("*hh*.xml"))
        meta = _parse_product_annotation(path)
        assert meta["n_lines"] == N_LINES
        assert meta["n_samples"] == N_SAMPLES
        assert meta["incidence"].min() == pytest.approx(20.0)
        assert meta["incidence"].max() == pytest.approx(45.0)

    def test_missing_calibration_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.xml"
        bad.write_text("<calibration></calibration>", encoding="utf-8")
        with pytest.raises(ValueError, match="No calibration vectors"):
            _parse_calibration(bad)


class TestSAFEProductReader:
    """End-to-end calibration on a synthetic product."""

    def test_lists_polarisations(self, safe_product: Path) -> None:
        assert SAFEProductReader(safe_product).available_polarisations() == ["HH", "HV"]

    def test_calibration_follows_the_specification(self, safe_product: Path) -> None:
        """sigma0 = DN^2 / A^2, minus the noise LUT scaled into the same units."""
        result = SAFEProductReader(safe_product).read_sigma0("HH", remove_thermal_noise=False)
        sigma0 = result["sigma0_linear"]
        expected = (1000.0**2) / (SIGMA_CONSTANT**2)
        assert float(sigma0[0, 0]) == pytest.approx(expected, rel=1e-4)
        assert float(sigma0[10, 20]) == pytest.approx((5000.0**2) / (SIGMA_CONSTANT**2), rel=1e-4)

    def test_noise_subtraction_lowers_the_floor(self, safe_product: Path) -> None:
        reader = SAFEProductReader(safe_product)
        raw = reader.read_sigma0("HH", remove_thermal_noise=False)["sigma0_linear"]
        denoised = reader.read_sigma0("HH", remove_thermal_noise=True)
        assert denoised["thermal_noise_removed"] is True

        # Tolerance is set by float32 resolution, not by the algorithm. The
        # calibrated background sits near sigma0 = 100, where a float32 ulp is
        # about 1.2e-5, and the noise drop being measured is only 4e-4. Two
        # rounded operands therefore admit roughly 2.4e-5 of absolute error.
        expected_drop = NOISE_CONSTANT / (SIGMA_CONSTANT**2)
        assert float(raw[0, 0] - denoised["sigma0_linear"][0, 0]) == pytest.approx(
            expected_drop, abs=3e-5
        )

    def test_geolocation_and_incidence_expand_to_raster(self, safe_product: Path) -> None:
        result = SAFEProductReader(safe_product).read_sigma0("HV")
        assert result["incidence_angle_deg"].shape == (N_LINES, N_SAMPLES)
        assert result["latitude"].shape == (N_LINES, N_SAMPLES)
        # Incidence increases across range, latitude across azimuth.
        assert result["incidence_angle_deg"][0, -1] > result["incidence_angle_deg"][0, 0]
        assert result["latitude"][-1, 0] > result["latitude"][0, 0]

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            SAFEProductReader(tmp_path / "nope.SAFE")

    def test_non_safe_directory_raises(self, tmp_path: Path) -> None:
        (tmp_path / "empty").mkdir()
        with pytest.raises(ValueError, match="does not look like a SAFE product"):
            SAFEProductReader(tmp_path / "empty")

    def test_unknown_polarisation_raises(self, safe_product: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Incomplete SAFE product"):
            SAFEProductReader(safe_product).read_sigma0("VV")
