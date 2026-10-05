"""Reader for Sentinel-1 Level-1 GRD products in SAFE format.

This is the piece that turns a downloaded ``.SAFE`` directory into calibrated
sigma-nought. It exists because the alternative â€” treating GRD digital numbers
as image intensities â€” is the single most common way SAR machine learning goes
wrong, and ADR-001 forbids it.

What a GRD product actually contains
------------------------------------
::

    S1x_EW_GRDM_1SDH_<start>_<stop>_<orbit>_<take>_<crc>.SAFE/
      measurement/
        s1x-ew-grd-hh-...-001.tiff        uint16 digital numbers
        s1x-ew-grd-hv-...-002.tiff
      annotation/
        s1x-ew-grd-hh-...-001.xml         geolocation grid, incidence angles
        calibration/
          calibration-s1x-ew-grd-hh-...xml    sigma/beta/gamma LUTs
          noise-s1x-ew-grd-hh-...xml          thermal noise LUTs

Calibration follows the ESA Sentinel-1 Product Specification: for a pixel with
digital number :math:`DN`, the calibrated backscatter is

.. math::

    \\sigma^0 = \\frac{DN^2}{A_{\\sigma}^2}

where :math:`A_{\\sigma}` is the sigmaNought LUT bilinearly interpolated from its
coarse (line, pixel) grid onto full raster resolution.

Thermal noise removal subtracts the noise LUT **in linear power**, before the
decibel conversion, because the noise floor is additive in power and not in
decibels. Negative results are retained rather than clipped at this stage; the
caller decides how to floor them, since clipping at zero biases the low tail of
the clutter distribution and would corrupt CFAR statistics over calm water.
"""

from __future__ import annotations

import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)

_POL_PATTERN = re.compile(r"-(hh|hv|vv|vh)-", re.IGNORECASE)


def _interp_extrapolate_1d(target: NDArray, coordinates: NDArray, values: NDArray) -> NDArray:
    """ESA linear interpolation continues the edge segment outside LUT knots."""
    result = np.asarray(np.interp(target, coordinates, values))
    if coordinates.size > 1:
        before, after = target < coordinates[0], target > coordinates[-1]
        result[before] = values[0] + (target[before] - coordinates[0]) * (values[1] - values[0]) / (
            coordinates[1] - coordinates[0]
        )
        result[after] = values[-1] + (target[after] - coordinates[-1]) * (
            values[-1] - values[-2]
        ) / (coordinates[-1] - coordinates[-2])
    return result


def _open_path(path: Path | str) -> str:
    """Format path with Windows extended-length prefix if needed."""
    resolved = str(Path(path).resolve())
    if sys.platform == "win32" and not resolved.startswith(("\\\\?\\", "//?/")):
        return f"\\\\?\\{resolved}"
    return resolved


#: Fraction of non-positive power above which a channel is reported unusable for
#: CFAR. Speckle legitimately produces a few very dark pixels, so a small
#: fraction is normal; tens of percent is a processing-quality rejection under
#: this research policy. It does not by itself establish the cause.
NONPOSITIVE_POWER_WARN_FRACTION = 0.05


@dataclass
class CalibrationLUT:
    """A Sentinel-1 calibration or noise lookup table on its coarse grid."""

    lines: NDArray[np.int64]
    pixels: NDArray[np.int64]
    values: NDArray[np.float64]

    def interpolate(
        self,
        shape: tuple[int, int],
        offset: tuple[int, int] = (0, 0),
        extrapolate_lines: bool = False,
    ) -> NDArray[np.float32]:
        """Bilinearly expand the LUT onto a full raster of ``shape``.

        The annotation grid is regular in line but the pixel axis is shared
        across rows, so a separable two-stage interpolation is exact here and far
        cheaper than a general scattered interpolation.
        """
        n_lines, n_samples = shape
        if min(shape) < 1 or not self.values.size or not np.isfinite(self.values).all():
            raise ValueError("LUT must contain finite samples and target shape must be positive.")
        unique_lines = np.unique(self.lines)
        unique_pixels = np.unique(self.pixels)

        grid = np.full((unique_lines.size, unique_pixels.size), np.nan, dtype=np.float64)
        line_index = {v: i for i, v in enumerate(unique_lines)}
        pixel_index = {v: i for i, v in enumerate(unique_pixels)}
        for ln, px, val in zip(self.lines, self.pixels, self.values, strict=True):
            grid[line_index[int(ln)], pixel_index[int(px)]] = val

        # Fill any holes along the pixel axis, then along the line axis.
        for r in range(grid.shape[0]):
            row = grid[r]
            ok = np.isfinite(row)
            if ok.any() and not ok.all():
                grid[r] = np.interp(unique_pixels, unique_pixels[ok], row[ok])

        target_lines = np.arange(n_lines, dtype=np.float64) + offset[0]
        target_pixels = np.arange(n_samples, dtype=np.float64) + offset[1]

        # Interpolate along pixels for each annotated line.
        by_line = np.empty((unique_lines.size, n_samples), dtype=np.float64)
        for r in range(unique_lines.size):
            by_line[r] = np.interp(target_pixels, unique_pixels.astype(np.float64), grid[r])

        # Then along lines for every output row, chunked to bound peak memory.
        src_lines = unique_lines.astype(np.float64)
        idx = np.interp(target_lines, src_lines, np.arange(src_lines.size, dtype=np.float64))
        if extrapolate_lines and src_lines.size > 1:
            lo = np.clip(
                np.searchsorted(src_lines, target_lines, side="right") - 1, 0, src_lines.size - 2
            )
            hi = lo + 1
            w = ((target_lines - src_lines[lo]) / (src_lines[hi] - src_lines[lo]))[:, None]
        else:
            lo = np.clip(np.floor(idx).astype(int), 0, src_lines.size - 1)
            hi = np.clip(lo + 1, 0, src_lines.size - 1)
            w = (idx - lo)[:, None]

        out = np.empty((n_lines, n_samples), dtype=np.float32)
        for c_start in range(0, n_samples, 4096):
            c_end = min(c_start + 4096, n_samples)
            block = by_line[:, c_start:c_end]
            out[:, c_start:c_end] = block[lo] * (1.0 - w) + block[hi] * w

        return np.asarray(out, dtype=np.float32)


@dataclass
class SwathAnnotation:
    """Per-polarisation annotation extracted from a SAFE product."""

    polarisation: str
    n_lines: int
    n_samples: int
    measurement_path: Path
    sigma_lut: CalibrationLUT
    noise_lut: CalibrationLUT | None
    incidence_grid_lines: NDArray[np.int64]
    incidence_grid_pixels: NDArray[np.int64]
    incidence_values: NDArray[np.float64]
    latitudes: NDArray[np.float64]
    longitudes: NDArray[np.float64]


def _parse_calibration(path: Path) -> tuple[CalibrationLUT, int, int]:
    """Parse a calibration annotation into a sigmaNought LUT."""
    root = ET.parse(_open_path(path)).getroot()
    lines: list[int] = []
    pixels: list[int] = []
    values: list[float] = []

    for vector in root.iter("calibrationVector"):
        line_el = vector.find("line")
        pixel_el = vector.find("pixel")
        sigma_el = vector.find("sigmaNought")
        if line_el is None or pixel_el is None or sigma_el is None:
            continue
        line = int((line_el.text or "0").strip())
        px = [int(v) for v in (pixel_el.text or "").split()]
        sg = [float(v) for v in (sigma_el.text or "").split()]
        if len(px) != len(sg):
            raise ValueError(f"Malformed calibration vector at line {line} in {path.name}")
        lines.extend([line] * len(px))
        pixels.extend(px)
        values.extend(sg)

    if not values:
        raise ValueError(f"No calibration vectors found in {path}")

    max_line = max(lines) + 1
    max_pixel = max(pixels) + 1
    return (
        CalibrationLUT(
            lines=np.asarray(lines, dtype=np.int64),
            pixels=np.asarray(pixels, dtype=np.int64),
            values=np.asarray(values, dtype=np.float64),
        ),
        max_line,
        max_pixel,
    )


def _parse_noise(path: Path) -> CalibrationLUT | None:
    """Parse a noise annotation into a range noise LUT, if present.

    Handles both the pre-IPF-2.90 ``noiseVector`` element and the later
    ``noiseRangeVector`` naming.
    """
    root = ET.parse(_open_path(path)).getroot()
    lines: list[int] = []
    pixels: list[int] = []
    values: list[float] = []

    for tag in ("noiseRangeVector", "noiseVector"):
        for vector in root.iter(tag):
            line_el = vector.find("line")
            pixel_el = vector.find("pixel")
            lut_el = vector.find("noiseRangeLut")
            if lut_el is None:
                lut_el = vector.find("noiseLut")
            if line_el is None or pixel_el is None or lut_el is None:
                continue
            line = int((line_el.text or "0").strip())
            px = [int(v) for v in (pixel_el.text or "").split()]
            nz = [float(v) for v in (lut_el.text or "").split()]
            if len(px) != len(nz):
                raise ValueError(f"Malformed noise vector in {path.name}")
            lines.extend([line] * len(px))
            pixels.extend(px)
            values.extend(nz)
        if values:
            break

    if not values:
        raise ValueError(f"No noise vectors found in {path.name}")

    return CalibrationLUT(
        lines=np.asarray(lines, dtype=np.int64),
        pixels=np.asarray(pixels, dtype=np.int64),
        values=np.asarray(values, dtype=np.float64),
    )


def _noise_power_grid(
    path: Path,
    shape: tuple[int, int],
    offset: tuple[int, int] = (0, 0),
    full_shape: tuple[int, int] | None = None,
) -> NDArray[np.float32]:
    """Multiply ESA range noise by the annotated azimuth factors (IPF >= 2.90).

    Older products supply a single range-only noise field. See ESA MPC-0392,
    Thermal Denoising of Products Generated by the S-1 IPF, section 5.2.
    """
    lut = _parse_noise(path)
    if lut is None:
        raise ValueError("Noise annotation is empty.")
    root = ET.parse(_open_path(path)).getroot()
    vectors = list(root.iter("noiseAzimuthVector"))
    if not vectors:
        power = lut.interpolate(shape, offset)
        if np.any(power < 0):
            raise ValueError("Noise power must be nonnegative.")
        return power
    power = np.full(shape, np.nan, dtype=np.float32)
    factors = np.full(shape, np.nan, dtype=np.float32)
    for vector in vectors:

        def required(tag: str, element: ET.Element = vector) -> str:
            value = element.findtext(tag)
            if value is None:
                raise ValueError(f"Missing {tag} in azimuth noise annotation.")
            return value

        r0, r1 = int(required("firstAzimuthLine")), int(required("lastAzimuthLine")) + 1
        c0, c1 = int(required("firstRangeSample")), int(required("lastRangeSample")) + 1
        lines = np.fromstring(required("line"), sep=" ", dtype=np.float64)
        values = np.fromstring(required("noiseAzimuthLut"), sep=" ", dtype=np.float64)
        if (
            not 0 <= r0 < r1 <= (full_shape or shape)[0]
            or not 0 <= c0 < c1 <= (full_shape or shape)[1]
            or lines.size != values.size
            or not lines.size
            or not np.isfinite(values).all()
            or np.any(values < 0)
            or np.any(np.diff(lines) <= 0)
        ):
            raise ValueError("Invalid azimuth noise vector.")
        wr0, wc0 = offset
        rr0, rr1 = max(r0, wr0), min(r1, wr0 + shape[0])
        cc0, cc1 = max(c0, wc0), min(c1, wc0 + shape[1])
        if rr1 > rr0 and cc1 > cc0:
            # ESA TOPS GRD selects range vectors belonging to this annotated
            # azimuth block, then extrapolates the bracketing vectors at the
            # block edges. Interpolating globally blends different blocks.
            in_block = (lut.lines >= r0) & (lut.lines < r1)
            if not in_block.any():
                raise ValueError("Azimuth block has no qualified range noise vectors.")
            block_lut = CalibrationLUT(
                lut.lines[in_block], lut.pixels[in_block], lut.values[in_block]
            )
            power[rr0 - wr0 : rr1 - wr0, cc0 - wc0 : cc1 - wc0] = block_lut.interpolate(
                (rr1 - rr0, cc1 - cc0), (rr0, cc0), extrapolate_lines=True
            )
            factors[rr0 - wr0 : rr1 - wr0, cc0 - wc0 : cc1 - wc0] = _interp_extrapolate_1d(
                np.arange(rr0, rr1), lines, values
            )[:, None]
    if not np.isfinite(factors).all() or np.any(factors < 0):
        raise ValueError("Azimuth noise annotation does not cover the full measurement grid.")
    if np.any(power < 0) or not np.isfinite(power).all():
        raise ValueError("Noise power must be finite and nonnegative across the measurement.")
    return np.asarray(power * factors, dtype=np.float32)


def _parse_product_annotation(path: Path) -> dict[str, Any]:
    """Parse raster dimensions and the geolocation grid from a product annotation."""
    root = ET.parse(_open_path(path)).getroot()

    n_lines_el = root.find(".//imageAnnotation/imageInformation/numberOfLines")
    n_samples_el = root.find(".//imageAnnotation/imageInformation/numberOfSamples")
    if n_lines_el is None or n_samples_el is None:
        raise ValueError(f"Missing image dimensions in {path.name}")

    g_lines: list[int] = []
    g_pixels: list[int] = []
    g_inc: list[float] = []
    g_lat: list[float] = []
    g_lon: list[float] = []

    def _child_float(element: ET.Element, tag: str) -> float:
        """Read a required numeric geolocation element."""
        el = element.find(tag)
        if el is None or not el.text:
            raise ValueError(f"Missing geolocation {tag} in {path.name}")
        value = float(el.text.strip())
        if not np.isfinite(value):
            raise ValueError(f"Nonfinite geolocation {tag} in {path.name}")
        return value

    for point in root.iter("geolocationGridPoint"):
        g_lines.append(int(_child_float(point, "line")))
        g_pixels.append(int(_child_float(point, "pixel")))
        g_inc.append(_child_float(point, "incidenceAngle"))
        g_lat.append(_child_float(point, "latitude"))
        g_lon.append(_child_float(point, "longitude"))

    if not g_lines:
        raise ValueError(f"No geolocation grid points in {path.name}")

    return {
        "n_lines": int((n_lines_el.text or "0").strip()),
        "n_samples": int((n_samples_el.text or "0").strip()),
        "grid_lines": np.asarray(g_lines, dtype=np.int64),
        "grid_pixels": np.asarray(g_pixels, dtype=np.int64),
        "incidence": np.asarray(g_inc, dtype=np.float64),
        "latitude": np.asarray(g_lat, dtype=np.float64),
        "longitude": np.asarray(g_lon, dtype=np.float64),
        "thermal_noise_corrected": root.findtext(
            ".//thermalNoiseCorrectionPerformed", "false"
        ).lower()
        == "true",
    }


def _polarisation_of(path: Path) -> str | None:
    """Extract the polarisation token from a SAFE filename."""
    m = _POL_PATTERN.search(path.name)
    return m.group(1).upper() if m else None


class SAFEProductReader:
    """Reads a Sentinel-1 GRD SAFE product into calibrated sigma-nought."""

    def __init__(self, safe_dir: Path | str) -> None:
        """Open a ``.SAFE`` directory and locate its annotation files."""
        self.safe_dir = Path(safe_dir)
        if not self.safe_dir.is_dir():
            raise FileNotFoundError(f"SAFE directory not found: {self.safe_dir}")

        self.measurement_dir = self.safe_dir / "measurement"
        self.annotation_dir = self.safe_dir / "annotation"
        if not self.measurement_dir.is_dir() or not self.annotation_dir.is_dir():
            raise ValueError(
                f"{self.safe_dir.name} does not look like a SAFE product "
                "(missing measurement/ or annotation/)."
            )
        match = re.search(r"S1[A-D]_(EW|IW)_GRD", self.safe_dir.name)
        if match is None:
            raise ValueError("Only Sentinel-1 EW/IW Level-1 GRD SAFE products are supported.")
        self.instrument_mode = match.group(1)

    def available_polarisations(self) -> list[str]:
        """List polarisations present in the product."""
        pols = set()
        for tif in self.measurement_dir.glob("*.tiff"):
            pol = _polarisation_of(tif)
            if pol:
                pols.add(pol)
        return sorted(pols)

    def _find_for_polarisation(self, polarisation: str) -> tuple[Path, Path, Path | None, Path]:
        """Locate measurement, product annotation, noise annotation and calibration files."""
        pol = polarisation.lower()

        def pick(paths: list[Path]) -> Path | None:
            matches = [p for p in paths if f"-{pol}-" in p.name.lower()]
            if len(matches) > 1:
                raise ValueError(f"Ambiguous {polarisation} SAFE component files")
            return matches[0] if matches else None

        measurement = pick(sorted(self.measurement_dir.glob("*.tiff")))
        annotation = pick(sorted(self.annotation_dir.glob("*.xml")))
        calib_dir = self.annotation_dir / "calibration"
        calibration = (
            pick(sorted(calib_dir.glob("calibration-*.xml"))) if calib_dir.is_dir() else None
        )
        noise = pick(sorted(calib_dir.glob("noise-*.xml"))) if calib_dir.is_dir() else None

        if measurement is None or annotation is None or calibration is None:
            raise FileNotFoundError(
                f"Incomplete SAFE product: could not find measurement/annotation/calibration "
                f"for polarisation {polarisation} in {self.safe_dir.name}"
            )
        return measurement, annotation, noise, calibration

    def read_sigma0(
        self,
        polarisation: str,
        remove_thermal_noise: bool = True,
        window: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        """Read one polarisation and return calibrated sigma-nought in linear power.

        Args:
            polarisation: One of HH, HV, VV, VH.
            remove_thermal_noise: Subtract the ESA noise LUT in linear power.

        Returns:
            A dictionary with ``sigma0_linear``, ``incidence_angle_deg``,
            ``latitude``, ``longitude`` and provenance metadata.
        """
        import rasterio

        measurement, annotation, noise_path, calibration_path = self._find_for_polarisation(
            polarisation
        )

        meta = _parse_product_annotation(annotation)
        full_shape = (meta["n_lines"], meta["n_samples"])
        r0, c0, height, width = window or (0, 0, *full_shape)
        if not (0 <= r0 < r0 + height <= full_shape[0] and 0 <= c0 < c0 + width <= full_shape[1]):
            raise ValueError("Window must lie within the measurement grid.")
        shape = (height, width)
        offset = (r0, c0)

        with rasterio.open(_open_path(measurement)) as src:
            if (src.height, src.width) != full_shape:
                raise ValueError("Annotation dimensions differ from measurement raster.")
            from rasterio.windows import Window

            raster_window = Window(c0, r0, width, height)
            dn = src.read(1, window=raster_window).astype(np.float64)
            valid = (src.read_masks(1, window=raster_window) > 0) & np.isfinite(dn) & (dn > 0)

        sigma_lut, _, _ = _parse_calibration(calibration_path)
        a_sigma = sigma_lut.interpolate(shape, offset).astype(np.float64)
        if not np.isfinite(a_sigma).all() or np.any(a_sigma <= 0):
            raise ValueError("Calibration LUT must be finite and positive.")

        sigma0 = (dn**2) / (a_sigma**2)

        noise_removed = bool(meta["thermal_noise_corrected"])
        if remove_thermal_noise and not noise_removed:
            if noise_path is None:
                raise ValueError("Thermal noise removal requested but noise annotation is missing.")
            noise_power = _noise_power_grid(noise_path, shape, offset, full_shape).astype(
                np.float64
            )
            # The noise LUT is expressed in DN^2, so it is divided by the same
            # calibration constant to reach sigma-nought power units.
            sigma0 = sigma0 - (noise_power / (a_sigma**2))
            noise_removed = True
        sigma0[~valid] = np.nan

        # Signed residual power diagnoses weak signal and model mismatch.
        # Never infer successful denoising from clipping it to a positive floor.
        finite = np.isfinite(sigma0)
        n_finite = int(finite.sum())
        nonpositive_fraction = (
            float(((sigma0 <= 0.0) & finite).sum() / n_finite) if n_finite else 1.0
        )
        if (
            window is None
            and noise_removed
            and nonpositive_fraction > NONPOSITIVE_POWER_WARN_FRACTION
        ):
            logger.warning(
                "%s %s: thermal noise removal left %.1f%% of finite pixels at non-positive "
                "power. The cause requires matched reference investigation; "
                "this channel is rejected for CFAR under the research quality policy "
                "(see ADR-007).",
                self.safe_dir.name,
                polarisation.upper(),
                100.0 * nonpositive_fraction,
            )

        incidence = _interp_scattered_grid(
            meta["grid_lines"], meta["grid_pixels"], meta["incidence"], shape, offset
        )
        latitude = _interp_scattered_grid(
            meta["grid_lines"], meta["grid_pixels"], meta["latitude"], shape, offset
        )
        longitude = _interp_scattered_grid(
            meta["grid_lines"], meta["grid_pixels"], meta["longitude"], shape, offset
        )

        logger.info(
            "Read %s %s: %dx%d, noise removal=%s",
            self.safe_dir.name,
            polarisation,
            shape[0],
            shape[1],
            noise_removed,
        )

        return {
            "sigma0_linear": np.asarray(sigma0, dtype=np.float32),
            "incidence_angle_deg": incidence,
            "latitude": latitude,
            "longitude": longitude,
            "polarisation": polarisation.upper(),
            "shape": shape,
            "window": (r0, c0, height, width),
            "full_shape": full_shape,
            "thermal_noise_removed": noise_removed,
            "nonpositive_power_fraction": nonpositive_fraction,
            "usable_for_cfar": noise_removed
            and n_finite > 0
            and nonpositive_fraction <= NONPOSITIVE_POWER_WARN_FRACTION,
            "product": self.safe_dir.name,
            "instrument_mode": self.instrument_mode,
        }


def _interp_scattered_grid(
    lines: NDArray[np.int64],
    pixels: NDArray[np.int64],
    values: NDArray[np.float64],
    shape: tuple[int, int],
    offset: tuple[int, int] = (0, 0),
) -> NDArray[np.float32]:
    """Interpolate a scattered annotation grid onto a full raster.

    The Sentinel-1 geolocation grid is rectangular in (line, pixel) even though
    it is stored as a flat list, so it is reshaped and interpolated separably.
    """
    return CalibrationLUT(lines, pixels, values).interpolate(shape, offset)
