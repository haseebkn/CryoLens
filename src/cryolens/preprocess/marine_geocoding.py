"""Marine range-Doppler inverse mapping with an explicit EGM96 height datum.

Uses annotated Earth-fixed orbit and GRD range polynomials, not fitted spatial
offsets. This is mean-sea-surface ellipsoid geocoding, not DEM terrain correction
or precise-orbit correction. See docs/processing-validation/v2/README.md.
"""

from __future__ import annotations

import hashlib
import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer
from scipy.interpolate import CubicHermiteSpline
from scipy.ndimage import map_coordinates

EGM96_ARCHIVE_SHA256 = "b5db7a14c3062decb8daaaf60f257ff374084be79aeda9ce333dd5b9c815751c"


@lru_cache(maxsize=1)
def _geoid_grid(archive: bytes) -> np.ndarray:
    if hashlib.sha256(archive).hexdigest() != EGM96_ARCHIVE_SHA256:
        raise ValueError("EGM96 archive differs from the pinned auxiliary-data receipt")
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        values = np.fromstring(source.read("ww15mgh_b.grd").decode("ascii"), sep=" ")
    if values.size != 6 + 721 * 1441 or not np.isfinite(values).all():
        raise ValueError("Invalid EGM96 grid")
    if not np.array_equal(values[:6], [-90, 90, 0, 360, 0.25, 0.25]):
        raise ValueError("Unsupported EGM96 coordinate convention")
    return values[6:].reshape(721, 1441)


def geoid_archive_path() -> Path:
    relative = Path("configs/auxdata/egm96/ww15mgh_b.zip")
    root = Path(__file__).resolve().parents[3] / relative
    if root.is_file():
        return root
    packaged = Path(__file__).resolve().parents[1] / "resources" / relative
    if not packaged.is_file():
        raise FileNotFoundError("The pinned EGM96 auxiliary archive is required")
    return packaged


class MarineGeocoder:
    """Inverse zero-Doppler mapping for declared, bistatic-corrected marine GRD."""

    def __init__(self, annotation: Path, geoid_archive: Path | None = None) -> None:
        self.annotation_sha256 = hashlib.sha256(annotation.read_bytes()).hexdigest()
        root = ET.parse(annotation).getroot()

        def required(node: Any, key: str) -> str:
            value = node.findtext(key) if node is not None else None
            if value is None:
                raise ValueError("Missing range-Doppler metadata: " + key)
            return str(value)

        info = root.find(".//imageInformation")
        epoch = datetime.fromisoformat(required(info, "productFirstLineUtcTime"))

        def seconds(text: str) -> float:
            return (datetime.fromisoformat(text) - epoch).total_seconds()

        self.full_shape = (
            int(required(info, "numberOfLines")),
            int(required(info, "numberOfSamples")),
        )
        self.interval = float(required(info, "azimuthTimeInterval"))
        self.spacing = float(required(info, "rangePixelSpacing"))
        self.near_slant_range = float(required(info, "slantRangeTime")) * 299792458 / 2
        if min(self.full_shape) < 2 or min(self.interval, self.spacing) <= 0:
            raise ValueError("Invalid GRD dimensions or pixel spacing")
        if root.findtext(".//bistaticDelayCorrectionApplied") != "true":
            raise ValueError("Marine geocoding requires declared bistatic-corrected timing")
        heights = [
            float(required(h, "value")) for h in root.findall(".//terrainHeightList/terrainHeight")
        ]
        if not heights or any(h != 0 for h in heights):
            raise ValueError("Only declared zero-reference marine heights are qualified")
        orbits = root.findall(".//orbitList/orbit")
        if len(orbits) < 4 or any(required(o, "frame") != "Earth Fixed" for o in orbits):
            raise ValueError("At least four Earth-fixed annotated orbit vectors are required")
        times = np.array([seconds(required(o, "time")) for o in orbits])
        positions = np.array([[float(required(o, "position/" + a)) for a in "xyz"] for o in orbits])
        velocities = np.array(
            [[float(required(o, "velocity/" + a)) for a in "xyz"] for o in orbits]
        )
        conversions = root.findall(".//coordinateConversionList/coordinateConversion")
        self.range_times = np.array([seconds(required(c, "azimuthTime")) for c in conversions])
        self.coefficients = np.array(
            [[float(v) for v in required(c, "grsrCoefficients").split()] for c in conversions]
        )
        self.origins = np.array([float(required(c, "gr0")) for c in conversions])
        if (
            len(conversions) < 2
            or self.coefficients.ndim != 2
            or self.coefficients.shape[1] < 2
            or any(
                not np.isfinite(a).all()
                for a in (
                    times,
                    positions,
                    velocities,
                    self.range_times,
                    self.coefficients,
                    self.origins,
                )
            )
            or not np.isfinite([self.interval, self.spacing, self.near_slant_range]).all()
            or self.near_slant_range <= 0
            or np.any(np.diff(times) <= 0)
            or np.any(np.diff(self.range_times) <= 0)
            or times[0] > 0
            or times[-1] < (self.full_shape[0] - 1) * self.interval
            or self.range_times[0] > 0
            or self.range_times[-1] < (self.full_shape[0] - 1) * self.interval
        ):
            raise ValueError("Nonfinite, unordered or insufficient orbit/range coverage")
        self.orbit_times = times
        self.orbit = CubicHermiteSpline(times, positions, velocities, extrapolate=False)
        self.geoid = _geoid_grid((geoid_archive or geoid_archive_path()).read_bytes())

    def provenance(self) -> dict[str, str]:
        """Explicit processing assumptions for exported artifacts and validation."""
        return {
            "geolocation_method": "marine_range_doppler",
            "height_datum": "EGM96_mean_sea_surface",
            "geoid_archive_sha256": EGM96_ARCHIVE_SHA256,
            "geometry_annotation_sha256": self.annotation_sha256,
            "orbit_source": "product_annotation",
            "bistatic_timing": "declared_bulk_plus_range_dependent_residual",
            "resampling_kernel": "bilinear_point",
        }

    def coordinates(
        self, longitude: np.ndarray, latitude: np.ndarray, window: tuple[int, int, int, int]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return source-centre indices and convergence/support mask; no extrapolation."""
        if (
            longitude.shape != latitude.shape
            or not np.isfinite(longitude).all()
            or not np.isfinite(latitude).all()
        ):
            raise ValueError("Destination coordinates must share a finite grid")
        if np.any(np.abs(latitude) > 90) or np.any(np.abs(longitude) > 180):
            raise ValueError("Destination coordinates outside WGS84 limits")
        r0, c0, height, width = window
        if not (
            0 <= r0 < r0 + height <= self.full_shape[0]
            and 0 <= c0 < c0 + width <= self.full_shape[1]
        ):
            raise ValueError("Geocoding window outside the annotated source")
        geoid = map_coordinates(
            self.geoid, [(90 - latitude) / 0.25, (longitude % 360) / 0.25], order=3, mode="nearest"
        )
        xyz = np.stack(
            Transformer.from_crs(4979, 4978, always_xy=True).transform(longitude, latitude, geoid),
            axis=-1,
        )
        time = np.full(latitude.shape, (r0 + height / 2) * self.interval)
        for _ in range(6):
            delta = xyz - self.orbit(time)
            velocity, acceleration = self.orbit(time, 1), self.orbit(time, 2)
            residual = np.sum(delta * velocity, axis=-1)
            derivative = np.sum(delta * acceleration - velocity * velocity, axis=-1)
            time = np.clip(time - residual / derivative, self.orbit_times[0], self.orbit_times[-1])
        position, velocity = self.orbit(time), self.orbit(time, 1)
        doppler_time_error = np.abs(np.sum((xyz - position) * velocity, axis=-1)) / np.sum(
            velocity**2, axis=-1
        )
        slant = np.linalg.norm(xyz - position, axis=-1)
        # S1 IPF corrects bulk timing at the near-range reference. The remaining
        # propagation delay varies with range; it is not a fitted image shift.
        time += (slant - self.near_slant_range) / 299792458
        slant = np.linalg.norm(xyz - self.orbit(time), axis=-1)
        coefficients = np.stack(
            [np.interp(time, self.range_times, column) for column in self.coefficients.T]
        )
        origin = np.interp(time, self.range_times, self.origins)
        ground = np.full(latitude.shape, (c0 + width / 2) * self.spacing)
        powers = np.arange(1, len(coefficients)).reshape((-1,) + (1,) * latitude.ndim)
        for _ in range(8):
            value = np.polynomial.polynomial.polyval(ground, coefficients, tensor=False)
            slope = np.polynomial.polynomial.polyval(
                ground, coefficients[1:] * powers, tensor=False
            )
            ground -= (value - slant) / slope
        range_error = np.abs(
            np.polynomial.polynomial.polyval(ground, coefficients, tensor=False) - slant
        )
        rows = time / self.interval - r0
        cols = (ground + origin) / self.spacing - c0
        valid = (
            np.isfinite(rows)
            & np.isfinite(cols)
            & (doppler_time_error <= 1e-6)
            & (range_error <= 0.1)
            & (time >= self.range_times[0])
            & (time <= self.range_times[-1])
            & (rows >= 0)
            & (rows <= height - 1)
            & (cols >= 0)
            & (cols <= width - 1)
        )
        return rows, cols, valid

    def resample(
        self,
        bands: dict[str, np.ndarray],
        valid: np.ndarray,
        window: tuple[int, int, int, int],
        destination: tuple[Any, int, int],
        target_crs: str,
        nodata: float,
    ) -> dict[str, np.ndarray]:
        """Bilinear point sampling in linear power, with bounded coordinate allocation."""
        transform, width, height = destination
        output = {name: np.full((height, width), nodata, dtype=np.float32) for name in bands}
        to_geographic = Transformer.from_crs(target_crs, 4326, always_xy=True)
        sources = {name: np.where(valid, values, np.nan) for name, values in bands.items()}
        for start in range(0, height, 64):
            rr, cc = np.indices((min(64, height - start), width))
            xx, yy = transform * (cc + 0.5, rr + start + 0.5)
            lon, lat = to_geographic.transform(xx, yy)
            row, col, supported = self.coordinates(lon, lat, window)
            sampled = {
                name: map_coordinates(
                    values, [row, col], order=1, mode="constant", cval=np.nan, prefilter=False
                )
                for name, values in sources.items()
            }
            for values in sampled.values():
                supported &= np.isfinite(values)
            for name, values in sampled.items():
                output[name][start : start + rr.shape[0]] = np.where(supported, values, nodata)
        return output
