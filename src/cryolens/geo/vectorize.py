"""Geographic vectorization and physical metric extraction from SAR detection masks."""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyproj
import rasterio.transform
import shapely.geometry
import shapely.ops
from skimage.measure import label, regionprops


@dataclass
class ExtractedTarget:
    """Vectorized SAR target detection with radiometric and geometric metrics."""

    target_id: int
    geom_epsg3978: shapely.geometry.Polygon
    geom_wgs84: shapely.geometry.Polygon
    centroid_wgs84: shapely.geometry.Point
    centroid_epsg3978: shapely.geometry.Point
    pixel_bbox: tuple[int, int, int, int]  # (min_row, min_col, max_row, max_col)
    pixel_area: int
    length_m: float
    width_m: float
    estimated_area_m2: float
    peak_sigma0_hv_db: float
    mean_sigma0_hv_db: float
    peak_sigma0_hh_db: float | None
    mean_sigma0_hh_db: float | None
    hh_hv_ratio_db: float | None
    incidence_angle_deg: float | None
    predicted_class: str
    confidence: float
    properties: dict[str, Any] = field(default_factory=dict)


def _local_half_pixel_degrees(
    latitude: np.ndarray,
    longitude: np.ndarray,
    row: int,
    col: int,
) -> tuple[float, float]:
    """Estimate half a pixel step in degrees at a given grid location.

    Sampled from the neighbouring geolocation entries rather than assumed, since
    degrees per pixel varies strongly with latitude across a 400 km EW swath.
    """
    h, w = latitude.shape
    r1 = row + 1 if row + 1 < h else max(0, row - 1)
    c1 = col + 1 if col + 1 < w else max(0, col - 1)

    # Both raster axes may carry latitude AND longitude on a rotated swath.
    dlon = abs(float(longitude[row, c1]) - float(longitude[row, col])) + abs(
        float(longitude[r1, col]) - float(longitude[row, col])
    )
    dlat = abs(float(latitude[r1, col]) - float(latitude[row, col])) + abs(
        float(latitude[row, c1]) - float(latitude[row, col])
    )

    # Fall back to a small nonzero epsilon on a degenerate 1-pixel grid.
    return (max(dlon, 1e-6) / 2.0, max(dlat, 1e-6) / 2.0)


class TargetVectorizer:
    """Transforms 2D binary CFAR hit masks into georeferenced polygons and physical metrics."""

    def __init__(
        self,
        source_crs: str = "EPSG:3978",
        target_crs: str = "EPSG:4326",
        min_pixels: int = 2,
    ) -> None:
        """Initialize vectorizer with coordinate transforms."""
        self.source_crs = source_crs
        self.target_crs = target_crs
        self.min_pixels = min_pixels
        if min_pixels < 1:
            raise ValueError("min_pixels must be positive")
        if pyproj.CRS(target_crs) != pyproj.CRS("EPSG:4326"):
            raise ValueError("target_crs must be EPSG:4326 for WGS84 output")
        source = pyproj.CRS(source_crs)
        if not source.is_projected or any(
            axis.unit_conversion_factor != 1.0 for axis in source.axis_info
        ):
            raise ValueError("source_crs must be a projected CRS in metres")

        self._transformer = pyproj.Transformer.from_crs(source_crs, target_crs, always_xy=True)

    def extract_targets(
        self,
        detection_mask: np.ndarray,
        transform: rasterio.transform.Affine | None,
        sigma0_hv_db: np.ndarray,
        sigma0_hh_db: np.ndarray | None = None,
        incidence_angle: np.ndarray | None = None,
        detector_name: str = "CA-CFAR",
        latitude: np.ndarray | None = None,
        longitude: np.ndarray | None = None,
        pixel_spacing_m: float | None = None,
    ) -> list[ExtractedTarget]:
        """Group connected pixels and extract physical properties for all detected targets.

        Two georeferencing modes are supported. Passing ``transform`` uses an
        affine mapping, appropriate for reprojected COGs. Passing ``latitude``
        and ``longitude`` per-pixel arrays instead uses tie-point geolocation
        directly, which is how Sentinel-1 products and the AI4Arctic
        distribution are referenced; interpolating those arrays is more faithful
        than fitting an affine to a curved swath geometry.
        """
        geolocated = latitude is not None and longitude is not None
        if (latitude is None) != (longitude is None):
            raise ValueError("Both latitude and longitude arrays are required")
        if detection_mask.ndim != 2 or detection_mask.size == 0:
            raise ValueError("detection_mask must be a nonempty 2D array")
        for arr in (sigma0_hv_db, sigma0_hh_db, incidence_angle, latitude, longitude):
            if arr is not None and arr.shape != detection_mask.shape:
                raise ValueError("All vectorization arrays must have matching shapes")
        if not geolocated and transform is None:
            raise ValueError(
                "extract_targets requires either an affine transform or "
                "latitude/longitude geolocation arrays."
            )

        # 8-connectivity connected component labeling
        usable = (
            np.asarray(detection_mask, dtype=bool)
            & np.isfinite(sigma0_hv_db)
            & (sigma0_hv_db > -90.0)
        )
        if geolocated:
            assert latitude is not None and longitude is not None
            usable &= np.isfinite(latitude) & np.isfinite(longitude)
            usable &= (np.abs(latitude) <= 90) & (np.abs(longitude) <= 180)
        labeled_mask = label(usable, connectivity=2)
        regions = regionprops(labeled_mask)

        if geolocated:
            if pixel_spacing_m is None:
                raise ValueError("pixel_spacing_m is required in geolocation-array mode.")
            if not np.isfinite(pixel_spacing_m) or pixel_spacing_m <= 0:
                raise ValueError("pixel_spacing_m must be finite and positive")
            px_res_x = px_res_y = float(pixel_spacing_m)
            pixel_area_m2 = float(pixel_spacing_m**2)
        else:
            # Pixel resolution in meters (under projected CRS EPSG:3978)
            assert transform is not None  # narrowed by the guard above
            px_res_x = float(np.hypot(transform.a, transform.d))
            px_res_y = float(np.hypot(transform.b, transform.e))
            pixel_area_m2 = abs(float(transform.a * transform.e - transform.b * transform.d))
            if not np.isfinite(pixel_area_m2) or pixel_area_m2 <= 0:
                raise ValueError("Affine transform must have finite nonzero pixel area")
        mean_px_spacing = (px_res_x + px_res_y) / 2.0

        to_projected = (
            pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3978", always_xy=True)
            if geolocated
            else None
        )

        targets: list[ExtractedTarget] = []

        for idx, region in enumerate(regions, start=1):
            if region.area < self.min_pixels:
                continue

            coords = region.coords  # (N, 2) array of [row, col]
            rows = coords[:, 0]
            cols = coords[:, 1]

            # Radiometric statistics on HV channel
            hv_vals = sigma0_hv_db[rows, cols]
            valid_hv = hv_vals[np.isfinite(hv_vals) & (hv_vals > -90.0)]
            if len(valid_hv) == 0:
                continue

            peak_hv = float(np.max(valid_hv))
            mean_hv = float(10.0 * np.log10(np.mean(10.0 ** (valid_hv / 10.0))))

            # HH channel
            if sigma0_hh_db is not None:
                hh_vals = sigma0_hh_db[rows, cols]
                valid_hh = hh_vals[np.isfinite(hh_vals) & (hh_vals > -90.0)]
                peak_hh = float(np.max(valid_hh)) if len(valid_hh) > 0 else None
                mean_hh = (
                    float(10.0 * np.log10(np.mean(10.0 ** (valid_hh / 10.0))))
                    if len(valid_hh) > 0
                    else None
                )
                # Ratios must compare paired samples, not peaks at different pixels.
                paired = (
                    np.isfinite(hh_vals)
                    & (hh_vals > -90.0)
                    & np.isfinite(hv_vals)
                    & (hv_vals > -90.0)
                )
                hh_hv_ratio = (
                    float(
                        10.0
                        * np.log10(
                            np.mean(10.0 ** (hh_vals[paired] / 10.0))
                            / np.mean(10.0 ** (hv_vals[paired] / 10.0))
                        )
                    )
                    if paired.any()
                    else None
                )
            else:
                peak_hh = mean_hh = hh_hv_ratio = None

            # Incidence angle
            if incidence_angle is not None:
                inc_vals = incidence_angle[rows, cols]
                valid_inc = inc_vals[np.isfinite(inc_vals) & (inc_vals > 0) & (inc_vals < 90)]
                mean_inc = float(np.mean(valid_inc)) if len(valid_inc) > 0 else None
            else:
                mean_inc = None

            # Geometric dimensions
            length_m = max(float(region.axis_major_length * mean_px_spacing), px_res_x)
            width_m = max(float(region.axis_minor_length * mean_px_spacing), px_res_y)
            if not geolocated:
                assert transform is not None
                # Transform the component's second moments, not an average
                # pixel spacing, for rotated, sheared or anisotropic rasters.
                dc, dr = cols - cols.mean(), rows - rows.mean()
                dx = transform.a * dc + transform.b * dr
                dy = transform.d * dc + transform.e * dr
                covariance = np.array(
                    [[np.mean(dx * dx), np.mean(dx * dy)], [np.mean(dx * dy), np.mean(dy * dy)]]
                )
                minor_var, major_var = np.maximum(np.linalg.eigvalsh(covariance), 0.0)
                floor = min(px_res_x, px_res_y)
                length_m = max(float(4 * np.sqrt(major_var)), floor)
                width_m = max(float(4 * np.sqrt(minor_var)), floor)
            area_m2 = float(region.area * pixel_area_m2)

            # Spatial georeferencing
            r_c, c_c = region.centroid
            min_r, min_c, max_r, max_c = region.bbox

            if geolocated:
                assert latitude is not None and longitude is not None
                assert to_projected is not None
                # Sample the interpolated geolocation arrays at the region's
                # pixel footprint. Clipped because region.bbox maxima are
                # exclusive in scikit-image.
                h, w = latitude.shape
                rr = np.clip(np.rint(rows).astype(int), 0, h - 1)
                cc = np.clip(np.rint(cols).astype(int), 0, w - 1)
                lats = latitude[rr, cc]
                lons = longitude[rr, cc]

                ri = int(np.clip(round(r_c), 0, h - 1))
                ci = int(np.clip(round(c_c), 0, w - 1))
                centroid_wgs84 = shapely.geometry.Point(float(np.mean(lons)), float(np.mean(lats)))

                lon_min, lon_max = float(np.nanmin(lons)), float(np.nanmax(lons))
                lat_min, lat_max = float(np.nanmin(lats)), float(np.nanmax(lats))

                # A region only one pixel wide in either axis would collapse to a
                # zero-area polygon, because every sampled coordinate is the same
                # pixel centre. A detected pixel covers a pixel-sized footprint,
                # so the box is grown by half a pixel using the local grid step.
                half_dlon, half_dlat = _local_half_pixel_degrees(latitude, longitude, ri, ci)
                lon_min, lon_max = lon_min - half_dlon, lon_max + half_dlon
                lat_min, lat_max = lat_min - half_dlat, lat_max + half_dlat

                poly_wgs84 = shapely.geometry.box(lon_min, lat_min, lon_max, lat_max)
                centroid_3978 = shapely.ops.transform(to_projected.transform, centroid_wgs84)
                poly_3978 = shapely.ops.transform(to_projected.transform, poly_wgs84)
            else:
                assert transform is not None
                # Pixel centroid -> EPSG:3978 coordinates
                x_c, y_c = rasterio.transform.xy(transform, r_c, c_c)
                centroid_3978 = shapely.geometry.Point(x_c, y_c)

                # bbox maxima are exclusive pixel edges; xy's centre default
                # previously shifted every footprint by half a pixel.
                poly_3978 = shapely.geometry.Polygon(
                    [
                        transform * (min_c, min_r),
                        transform * (max_c, min_r),
                        transform * (max_c, max_r),
                        transform * (min_c, max_r),
                    ]
                )

                # Reproject to EPSG:4326 (WGS84) for GeoJSON
                centroid_wgs84 = shapely.ops.transform(self._transformer.transform, centroid_3978)
                poly_wgs84 = shapely.ops.transform(self._transformer.transform, poly_3978)
                if pyproj.CRS(self.source_crs) != pyproj.CRS("EPSG:3978"):
                    projected = pyproj.Transformer.from_crs(
                        self.source_crs, "EPSG:3978", always_xy=True
                    )
                    centroid_3978 = shapely.ops.transform(projected.transform, centroid_3978)
                    poly_3978 = shapely.ops.transform(projected.transform, poly_3978)

            # Heuristic classification
            # High cross-pol volume scattering + moderate ratio -> Iceberg
            # Intense co-pol point-reflection + high ratio -> Vessel/Ship
            if peak_hv >= -24.0 and hh_hv_ratio is not None and hh_hv_ratio <= 15.0:
                pred_class = "iceberg"
                conf = min(0.95, 0.50 + max(0.0, (peak_hv + 24.0) * 0.03))
            elif (
                peak_hh is not None
                and hh_hv_ratio is not None
                and peak_hh >= -10.0
                and hh_hv_ratio > 15.0
                and region.area <= 10
            ):
                pred_class = "ship"
                conf = min(0.90, 0.50 + max(0.0, (peak_hh + 10.0) * 0.03))
            else:
                pred_class = "clutter"
                conf = 0.50

            targets.append(
                ExtractedTarget(
                    target_id=idx,
                    geom_epsg3978=poly_3978,
                    geom_wgs84=poly_wgs84,
                    centroid_wgs84=centroid_wgs84,
                    centroid_epsg3978=centroid_3978,
                    pixel_bbox=(min_r, min_c, max_r, max_c),
                    pixel_area=int(region.area),
                    length_m=round(length_m, 1),
                    width_m=round(width_m, 1),
                    estimated_area_m2=round(area_m2, 1),
                    peak_sigma0_hv_db=round(peak_hv, 2),
                    mean_sigma0_hv_db=round(mean_hv, 2),
                    peak_sigma0_hh_db=None if peak_hh is None else round(peak_hh, 2),
                    mean_sigma0_hh_db=None if mean_hh is None else round(mean_hh, 2),
                    hh_hv_ratio_db=None if hh_hv_ratio is None else round(hh_hv_ratio, 2),
                    incidence_angle_deg=None if mean_inc is None else round(mean_inc, 1),
                    predicted_class="unclassified",
                    confidence=round(conf, 3),
                    properties={
                        "detector": detector_name,
                        "pixel_centroid": [round(r_c, 2), round(c_c, 2)],
                        "heuristic_class": pred_class,
                        "confidence_kind": "uncalibrated_heuristic_score",
                        "validation_status": "unverified",
                        "geometry_kind": "pixel_envelope",
                        "dimensions_kind": "SAR_response_extent_not_physical_iceberg_size",
                    },
                )
            )

        return targets
