"""Analytic orbit/range cases and observable sampling behavior; no satellite truth claims."""

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from rasterio.transform import from_origin

from cryolens.preprocess.marine_geocoding import MarineGeocoder, geoid_archive_path


@pytest.fixture
def annotation(tmp_path: Path) -> Path:
    orbit = "".join(
        f"<orbit><time>2018-04-28T00:{minute:02}:{second:02}</time><frame>Earth Fixed</frame>"
        f"<position><x>7000000</x><y>{7500 * time}</y><z>0</z></position>"
        "<velocity><x>0</x><y>7500</y><z>0</z></velocity></orbit>"
        for time, minute, second in [(-20, 0, 40), (0, 1, 0), (20, 1, 20), (40, 1, 40)]
    )
    path = tmp_path / "analytic.xml"
    path.write_text(
        "<product><imageInformation><productFirstLineUtcTime>2018-04-28T00:01:00</productFirstLineUtcTime>"
        "<numberOfLines>100</numberOfLines><numberOfSamples>10000</numberOfSamples>"
        "<azimuthTimeInterval>0.01</azimuthTimeInterval><rangePixelSpacing>10</rangePixelSpacing>"
        "<slantRangeTime>0.004002769142377824</slantRangeTime></imageInformation>"
        "<bistaticDelayCorrectionApplied>true</bistaticDelayCorrectionApplied>"
        "<terrainHeightList><terrainHeight><value>0</value></terrainHeight></terrainHeightList>"
        f"<orbitList>{orbit}</orbitList><coordinateConversionList>"
        + "".join(
            f"<coordinateConversion><azimuthTime>{time}</azimuthTime><gr0>0</gr0>"
            "<grsrCoefficients>600000 1</grsrCoefficients></coordinateConversion>"
            for time in ["2018-04-28T00:00:59", "2018-04-28T00:01:02"]
        )
        + "</coordinateConversionList></product>"
    )
    return path


def test_inverse_mapping_solves_analytic_orbit_and_respects_window(annotation: Path) -> None:
    model = MarineGeocoder(annotation)
    row, col, valid = model.coordinates(np.array([0.0]), np.array([0.0]), (0, 0, 100, 10000))
    # EGM96 at the equator/Greenwich grid knot is 17.162 m. The orthogonal
    # straight orbit crosses at t=0 and range = 7,000,000 - (WGS84 a + N).
    delay = (7000000 - 6378137 - 17.162 - 600000) / 299792458
    np.testing.assert_allclose(row, delay / 0.01, atol=1e-10)
    np.testing.assert_allclose(col, (7000000 - 6378137 - 17.162 - 600000) / 10, atol=1e-7)
    assert valid.all()
    _, clipped, supported = model.coordinates(np.array([0.0]), np.array([0.0]), (0, 2180, 100, 10))
    np.testing.assert_allclose(clipped, col - 2180, atol=1e-7)
    assert supported.all()
    _, _, outside = model.coordinates(np.array([5.0]), np.array([0.0]), (0, 0, 100, 10000))
    assert not outside.any()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("<frame>Earth Fixed</frame>", "<frame>Inertial</frame>", "Earth-fixed"),
        ("<value>0</value>", "<value>1</value>", "marine heights"),
        ("Applied>true", "Applied>false", "bistatic"),
        ("<rangePixelSpacing>10", "<rangePixelSpacing>nan", "Nonfinite"),
    ],
)
def test_unqualified_geometry_is_rejected(
    annotation: Path, old: str, new: str, message: str
) -> None:
    annotation.write_text(annotation.read_text().replace(old, new))
    with pytest.raises(ValueError, match=message):
        MarineGeocoder(annotation)


def test_changed_auxiliary_data_cannot_be_used(annotation: Path, tmp_path: Path) -> None:
    corrupt = tmp_path / "egm96.zip"
    corrupt.write_bytes(geoid_archive_path().read_bytes() + b"changed")
    with pytest.raises(ValueError, match="pinned"):
        MarineGeocoder(annotation, corrupt)


def test_point_kernel_preserves_invalid_neighbors_and_does_not_area_average(
    annotation: Path,
) -> None:
    model = MarineGeocoder(annotation)
    signal = np.arange(36, dtype=np.float32).reshape(6, 6)
    valid = np.ones_like(signal, dtype=bool)
    valid[4, 4] = False
    # Literal source-centre positions define the public resampler contract.
    # For the first destination point, the four samples are 7,8,13,14 -> 10.5.
    with patch.object(
        model,
        "coordinates",
        return_value=(np.array([[1.5, 3.5]]), np.array([[1.5, 3.5]]), np.ones((1, 2), dtype=bool)),
    ):
        result = model.resample(
            {"power": signal},
            valid,
            (0, 0, 6, 6),
            (from_origin(0, 0, 40, 40), 2, 1),
            "EPSG:3978",
            -9999,
        )
    assert result["power"][0, 0] == 10.5
    assert result["power"][0, 1] == -9999


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows extended path contract")
def test_geocoder_accepts_long_annotation_path(annotation: Path, tmp_path: Path) -> None:
    from cryolens.preprocess.safe_reader import _open_path

    folder = tmp_path / ("a" * 90) / ("b" * 90)
    extended = Path(_open_path(folder))
    extended.mkdir(parents=True)
    ordinary = folder / "annotation.xml"
    Path(_open_path(ordinary)).write_bytes(annotation.read_bytes())
    model = MarineGeocoder(ordinary)
    row, col, valid = model.coordinates(np.array([0.0]), np.array([0.0]), (0, 0, 100, 10000))
    assert valid.all()
    assert np.isfinite(row).all() and np.isfinite(col).all()
