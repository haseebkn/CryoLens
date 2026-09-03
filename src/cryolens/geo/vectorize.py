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
    peak_sigma0_hh_db: float
    mean_sigma0_hh_db: float
    hh_hv_ratio_db: float
    incidence_angle_deg: float
    predicted_class: str
    confidence: float
    properties: dict[str, Any] = field(default_factory=dict)


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
        if not geolocated and transform is None:
            raise ValueError(
                "extract_targets requires either an affine transform or "
                "latitude/longitude geolocation arrays."
            )

        # 8-connectivity connected component labeling
        labeled_mask = label(detection_mask, connectivity=2)
        regions = regionprops(labeled_mask)

        if geolocated:
            if pixel_spacing_m is None:
                raise ValueError("pixel_spacing_m is required in geolocation-array mode.")
            px_res_x = px_res_y = float(pixel_spacing_m)
        else:
            # Pixel resolution in meters (under projected CRS EPSG:3978)
            assert transform is not None  # narrowed by the guard above
            px_res_x = float(abs(transform.a))
            px_res_y = float(abs(transform.e))
        pixel_area_m2 = px_res_x * px_res_y
        mean_px_spacing = (px_res_x + px_res_y) / 2.0

        to_projected = (
            pyproj.Transformer.from_crs(self.target_crs, self.source_crs, always_xy=True)
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
            mean_hv = float(np.mean(valid_hv))

            # HH channel
            if sigma0_hh_db is not None:
                hh_vals = sigma0_hh_db[rows, cols]
                valid_hh = hh_vals[np.isfinite(hh_vals) & (hh_vals > -90.0)]
                peak_hh = float(np.max(valid_hh)) if len(valid_hh) > 0 else peak_hv
                mean_hh = float(np.mean(valid_hh)) if len(valid_hh) > 0 else mean_hv
            else:
                peak_hh = peak_hv
                mean_hh = mean_hv

            hh_hv_ratio = peak_hh - peak_hv

            # Incidence angle
            if incidence_angle is not None:
                inc_vals = incidence_angle[rows, cols]
                valid_inc = inc_vals[np.isfinite(inc_vals)]
                mean_inc = float(np.mean(valid_inc)) if len(valid_inc) > 0 else 35.0
            else:
                mean_inc = 35.0

            # Geometric dimensions
            length_m = max(float(region.axis_major_length * mean_px_spacing), px_res_x)
            width_m = max(float(region.axis_minor_length * mean_px_spacing), px_res_y)
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
                centroid_wgs84 = shapely.geometry.Point(
                    float(longitude[ri, ci]), float(latitude[ri, ci])
                )
                poly_wgs84 = shapely.geometry.box(
                    float(np.nanmin(lons)),
                    float(np.nanmin(lats)),
                    float(np.nanmax(lons)),
                    float(np.nanmax(lats)),
                )
                centroid_3978 = shapely.ops.transform(to_projected.transform, centroid_wgs84)
                poly_3978 = shapely.ops.transform(to_projected.transform, poly_wgs84)
            else:
                assert transform is not None
                # Pixel centroid -> EPSG:3978 coordinates
                x_c, y_c = rasterio.transform.xy(transform, r_c, c_c)
                centroid_3978 = shapely.geometry.Point(x_c, y_c)

                x_min, y_max = rasterio.transform.xy(transform, min_r, min_c)
                x_max, y_min = rasterio.transform.xy(transform, max_r, max_c)

                poly_3978 = shapely.geometry.box(
                    min(x_min, x_max),
                    min(y_min, y_max),
                    max(x_min, x_max),
                    max(y_min, y_max),
                )

                # Reproject to EPSG:4326 (WGS84) for GeoJSON
                centroid_wgs84 = shapely.ops.transform(self._transformer.transform, centroid_3978)
                poly_wgs84 = shapely.ops.transform(self._transformer.transform, poly_3978)

            # Heuristic classification
            # High cross-pol volume scattering + moderate ratio -> Iceberg
            # Intense co-pol point-reflection + high ratio -> Vessel/Ship
            if peak_hv >= -24.0 and hh_hv_ratio <= 15.0:
                pred_class = "iceberg"
                conf = min(0.95, 0.50 + max(0.0, (peak_hv + 24.0) * 0.03))
            elif peak_hh >= -10.0 and hh_hv_ratio > 15.0 and region.area <= 10:
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
                    pixel_area=region.area,
                    length_m=round(length_m, 1),
                    width_m=round(width_m, 1),
                    estimated_area_m2=round(area_m2, 1),
                    peak_sigma0_hv_db=round(peak_hv, 2),
                    mean_sigma0_hv_db=round(mean_hv, 2),
                    peak_sigma0_hh_db=round(peak_hh, 2),
                    mean_sigma0_hh_db=round(mean_hh, 2),
                    hh_hv_ratio_db=round(hh_hv_ratio, 2),
                    incidence_angle_deg=round(mean_inc, 1),
                    predicted_class=pred_class,
                    confidence=round(conf, 3),
                    properties={
                        "detector": detector_name,
                        "pixel_centroid": [round(r_c, 2), round(c_c, 2)],
                    },
                )
            )

        return targets
