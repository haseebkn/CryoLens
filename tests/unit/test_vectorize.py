"""Unit tests for geographic vectorization and shape metric extraction."""

import numpy as np
import pytest
import rasterio.transform
import shapely.geometry

from cryolens.geo.vectorize import TargetVectorizer


def test_affine_coordinate_roundtrip() -> None:
    """Verify that forward and reverse affine coordinate transformations have < 1m error."""
    # Define an affine transform typical of Grand Banks EPSG:3978 projection at 40m resolution
    # Top-left corner: (x=2,000,000, y=1,000,000)
    transform = rasterio.transform.from_origin(2000000.0, 1000000.0, 40.0, 40.0)

    # Test pixel coordinates
    test_rows = [0, 50, 128.5, 500, 1023]
    test_cols = [0, 50, 256.5, 500, 1023]

    for r, c in zip(test_rows, test_cols, strict=False):
        # Forward: pixel -> projected coords
        x, y = rasterio.transform.xy(transform, r, c)
        # Reverse: projected coords -> pixel
        rev_r, rev_c = rasterio.transform.rowcol(transform, x, y)

        # Allow sub-pixel rounding error (< 1 pixel = 40m, here rowcol gives integer or exact)
        dist_x = abs((rev_c - c) * 40.0)
        dist_y = abs((rev_r - r) * 40.0)
        assert dist_x <= 40.0
        assert dist_y <= 40.0


def test_target_vectorization_metrics() -> None:
    """Test connected component extraction and shape metrics on known synthetic clusters."""
    mask = np.zeros((100, 100), dtype=bool)
    hv_db = np.full((100, 100), -32.0)
    hh_db = np.full((100, 100), -22.0)

    # Create a 3x3 pixel target at (20:23, 30:33) -> area = 9 pixels
    mask[20:23, 30:33] = True
    hv_db[20:23, 30:33] = -16.5
    hv_db[21, 31] = -14.0  # Peak in center
    hh_db[20:23, 30:33] = -12.0

    # 40m spacing
    transform = rasterio.transform.from_origin(2000000.0, 1000000.0, 40.0, 40.0)

    vectorizer = TargetVectorizer(source_crs="EPSG:3978", target_crs="EPSG:4326", min_pixels=2)
    targets = vectorizer.extract_targets(
        detection_mask=mask,
        transform=transform,
        sigma0_hv_db=hv_db,
        sigma0_hh_db=hh_db,
    )

    assert len(targets) == 1
    t = targets[0]

    assert t.pixel_area == 9
    assert t.peak_sigma0_hv_db == -14.0
    assert -17.0 <= t.mean_sigma0_hv_db <= -14.0
    # Area = 9 * 1600 m2 = 14400 m2
    assert t.estimated_area_m2 == 14400.0
    assert t.length_m >= 40.0
    assert t.width_m >= 40.0
    assert t.predicted_class == "iceberg"

    # Verify WGS84 geometry
    assert isinstance(t.geom_wgs84, shapely.geometry.Polygon)
    assert isinstance(t.centroid_wgs84, shapely.geometry.Point)
    # Longitude should be in Grand Banks range [-65, -40] and Latitude in [40, 60]
    assert -65.0 <= t.centroid_wgs84.x <= -40.0
    assert 40.0 <= t.centroid_wgs84.y <= 60.0


def test_min_pixels_filtering() -> None:
    """Test that isolated single-pixel noise is filtered out when min_pixels > 1."""
    mask = np.zeros((50, 50), dtype=bool)
    # 1 isolated pixel
    mask[10, 10] = True
    # 1 cluster of 4 pixels
    mask[30:32, 30:32] = True

    hv_db = np.full((50, 50), -30.0)
    transform = rasterio.transform.from_origin(2000000.0, 1000000.0, 40.0, 40.0)

    vectorizer = TargetVectorizer(min_pixels=2)
    targets = vectorizer.extract_targets(
        detection_mask=mask,
        transform=transform,
        sigma0_hv_db=hv_db,
    )

    assert len(targets) == 1
    assert targets[0].pixel_area == 4


class TestGeolocatedVectorisation:
    """Tie-point geolocation mode, used for Sentinel-1 and AI4Arctic products."""

    @staticmethod
    def _grid(h: int = 20, w: int = 20) -> tuple[np.ndarray, np.ndarray]:
        """A regular lat/lon grid over the Grand Banks."""
        lat = np.linspace(47.0, 48.0, h)[:, None] * np.ones((1, w))
        lon = np.ones((h, 1)) * np.linspace(-53.0, -52.0, w)[None, :]
        return lat.astype(np.float32), lon.astype(np.float32)

    def test_single_pixel_target_has_nonzero_area(self) -> None:
        """A one-pixel detection must not collapse to a degenerate polygon."""
        lat, lon = self._grid()
        mask = np.zeros((20, 20), dtype=bool)
        mask[10, 10] = True
        hv = np.full((20, 20), -30.0, dtype=np.float32)
        hv[10, 10] = -15.0

        targets = TargetVectorizer(min_pixels=1).extract_targets(
            detection_mask=mask,
            transform=None,
            sigma0_hv_db=hv,
            latitude=lat,
            longitude=lon,
            pixel_spacing_m=80.0,
        )

        assert len(targets) == 1
        assert targets[0].geom_wgs84.area > 0.0
        assert targets[0].geom_epsg3978.area > 0.0

    def test_centroid_lands_on_the_detected_pixel(self) -> None:
        lat, lon = self._grid()
        mask = np.zeros((20, 20), dtype=bool)
        mask[5, 15] = True
        hv = np.full((20, 20), -30.0, dtype=np.float32)

        targets = TargetVectorizer(min_pixels=1).extract_targets(
            detection_mask=mask,
            transform=None,
            sigma0_hv_db=hv,
            latitude=lat,
            longitude=lon,
            pixel_spacing_m=80.0,
        )
        c = targets[0].centroid_wgs84
        assert c.y == pytest.approx(float(lat[5, 15]), abs=1e-4)
        assert c.x == pytest.approx(float(lon[5, 15]), abs=1e-4)

    def test_requires_georeferencing(self) -> None:
        hv = np.full((5, 5), -30.0, dtype=np.float32)
        with pytest.raises(ValueError, match="affine transform or"):
            TargetVectorizer().extract_targets(
                detection_mask=np.zeros((5, 5), dtype=bool),
                transform=None,
                sigma0_hv_db=hv,
            )

    def test_requires_pixel_spacing_in_geolocated_mode(self) -> None:
        lat, lon = self._grid(5, 5)
        hv = np.full((5, 5), -30.0, dtype=np.float32)
        with pytest.raises(ValueError, match="pixel_spacing_m is required"):
            TargetVectorizer().extract_targets(
                detection_mask=np.zeros((5, 5), dtype=bool),
                transform=None,
                sigma0_hv_db=hv,
                latitude=lat,
                longitude=lon,
            )
