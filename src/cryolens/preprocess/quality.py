"""Fail-closed research processing checks; thresholds are not sensor certification."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from rasterio.errors import RasterioError

from cryolens.preprocess.safe_reader import SAFEProductReader, _parse_product_annotation


class QualityPolicy(BaseModel):
    """Versioned conservative acceptance limits, fixed before comparing products."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = "safe-quality-v1"
    max_nonpositive_fraction: float = Field(default=0.05, ge=0, lt=1)
    min_valid_fraction: float = Field(default=0.1, gt=0, le=1)
    incidence_min_deg: float = 15.0
    incidence_max_deg: float = 50.0
    max_polarization_geolocation_error_m: float = 20.0


class ProcessingQualityError(ValueError):
    """Carries machine-readable rejection reasons instead of a plausible output."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("Processing quality rejected: " + "; ".join(report["reasons"]))


def assess_channels(
    hh: dict[str, Any], hv: dict[str, Any], policy: QualityPolicy | None = None
) -> dict[str, Any]:
    """Assess actual inputs including correction, emptiness and co-registration."""
    from pyproj import Geod

    policy = policy or QualityPolicy()
    reasons: list[str] = []
    channels: dict[str, Any] = {}
    for label, channel in (("HH", hh), ("HV", hv)):
        power = np.asarray(channel["sigma0_linear"])
        finite = np.isfinite(power)
        count = int(finite.sum())
        fraction = float(((power <= 0) & finite).sum() / count) if count else 1.0
        valid_fraction = float(finite.mean()) if finite.size else 0.0
        angles = np.asarray(channel["incidence_angle_deg"])
        angle_bad = finite & (
            ~np.isfinite(angles)
            | (angles < policy.incidence_min_deg)
            | (angles > policy.incidence_max_deg)
        )
        channels[label] = {
            "finite_pixels": count,
            "nonpositive_fraction": fraction,
            "valid_fraction": valid_fraction,
            "invalid_incidence_pixels": int(angle_bad.sum()),
        }
        if not channel.get("thermal_noise_removed", False):
            reasons.append(f"{label}: thermal noise correction not established")
        if valid_fraction < policy.min_valid_fraction:
            reasons.append(f"{label}: insufficient valid observations")
        if fraction > policy.max_nonpositive_fraction:
            reasons.append(
                f"{label}: non-positive power fraction {fraction:.6f} exceeds {policy.max_nonpositive_fraction}"
            )
        if angle_bad.any():
            reasons.append(f"{label}: invalid incidence angles")
    if hh["shape"] != hv["shape"] or hh.get("window") != hv.get("window"):
        reasons.append("polarization grids differ")
    else:
        lat1, lon1 = np.asarray(hh["latitude"]), np.asarray(hh["longitude"])
        lat2, lon2 = np.asarray(hv["latitude"]), np.asarray(hv["longitude"])
        if not lat1.size or not all(np.isfinite(a).all() for a in (lat1, lon1, lat2, lon2)):
            reasons.append("nonfinite geolocation")
        elif any(
            np.any(np.abs(a) > limit)
            for a, limit in ((lat1, 90), (lat2, 90), (lon1, 180), (lon2, 180))
        ):
            reasons.append("geolocation outside WGS84 limits")
        else:
            # Sparse deterministic check bounds work on full arrays too.
            step = max(1, lat1.size // 10000)
            _, _, distance = Geod(ellps="WGS84").inv(
                lon1.ravel()[::step],
                lat1.ravel()[::step],
                lon2.ravel()[::step],
                lat2.ravel()[::step],
            )
            if float(np.max(distance)) > policy.max_polarization_geolocation_error_m:
                reasons.append("polarization geolocation mismatch")
    return {
        "schema_version": 1,
        "policy": policy.model_dump(),
        "product": hh.get("product"),
        "window": hh.get("window"),
        "channels": channels,
        "passed": not reasons,
        "reasons": reasons,
    }


def require_channels(
    hh: dict[str, Any], hv: dict[str, Any], report_path: Path | None = None
) -> dict[str, Any]:
    """Persist rejection before raising so failed processing remains auditable."""
    report = assess_channels(hh, hv)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    if not report["passed"]:
        raise ProcessingQualityError(report)
    return report


def record_processing_failure(path: Path, product: str, stage: str, error: Exception) -> None:
    """Keep a rejection receipt even for failures before reading or after warping."""
    report: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else {
            "schema_version": 1,
            "product": product,
            "reasons": [],
        }
    )
    report.update(passed=False, failed_stage=stage)
    if str(error) not in report["reasons"]:
        report["reasons"].append(str(error))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")


def require_detection_ready_cog(dataset: Any) -> None:
    """Reject fresh SAFE outputs without an explicit passed processing receipt."""
    tags = dataset.tags()
    fresh = tags.get("source_kind") == "sentinel1_safe" or re.match(
        r"S1[A-D]_(EW|IW)_GRD", Path(dataset.name).name
    )
    if fresh and tags.get("processing_quality") != "passed":
        raise ValueError("Fresh SAFE COG has no passed processing-quality gate")
    if fresh and tags.get("fresh_detection_gate") != "passed":
        raise ValueError(
            "Fresh SAFE detection deployment remains blocked pending scientific validation"
        )
    if tags.get("source_kind") == "synthetic":
        raise ValueError("Synthetic COGs cannot enter real-observation detection")
    if dataset.descriptions != ("sigma0_hh_db", "sigma0_hv_db", "ratio_hh_hv", "incidence_angle"):
        raise ValueError("Detection requires explicitly described HH/HV calibrated bands")


def assess_safe_product(
    reader: SAFEProductReader, report_path: Path, block_rows: int = 256
) -> dict[str, Any]:
    """Visit every measured pixel in bounded stripes before allocating a full scene."""
    if block_rows < 1:
        raise ValueError("Block rows must be positive")
    policy = QualityPolicy()
    report: dict[str, Any] = {
        "schema_version": 1,
        "policy": policy.model_dump(),
        "product": reader.safe_dir.name,
        "instrument_mode": reader.instrument_mode,
        "scope": "full_measurement_grid",
        "channels": {},
        "stripes": [],
        "reasons": [],
        "passed": False,
    }
    try:
        if not (reader.safe_dir / "manifest.safe").is_file():
            raise ValueError("SAFE manifest is missing")
        if not {"HH", "HV"}.issubset(reader.available_polarisations()):
            raise ValueError("Research detector requires HH/HV; VV/VH is not interchangeable")
        meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
        height, width = meta["n_lines"], meta["n_samples"]
        totals = {
            p: {"finite_pixels": 0, "nonpositive_pixels": 0, "invalid_incidence_pixels": 0}
            for p in ("HH", "HV")
        }
        for start in range(0, height, block_rows):
            window = (start, 0, min(block_rows, height - start), width)
            hh, hv = (reader.read_sigma0(p, window=window) for p in ("HH", "HV"))
            stripe = assess_channels(hh, hv, policy)
            report["stripes"].append({"window": window, "channels": stripe["channels"]})
            for label, channel in (("HH", hh), ("HV", hv)):
                values = channel["sigma0_linear"]
                finite = np.isfinite(values)
                totals[label]["finite_pixels"] += int(finite.sum())
                totals[label]["nonpositive_pixels"] += int(np.count_nonzero(finite & (values <= 0)))
                totals[label]["invalid_incidence_pixels"] += stripe["channels"][label][
                    "invalid_incidence_pixels"
                ]
            for reason in stripe["reasons"]:
                if (
                    "geolocation" in reason
                    or "grids differ" in reason
                    or "correction not" in reason
                ):
                    if reason not in report["reasons"]:
                        report["reasons"].append(reason)
        for label, counts in totals.items():
            count = counts["finite_pixels"]
            fraction = counts["nonpositive_pixels"] / count if count else 1.0
            valid_fraction = count / (height * width)
            report["channels"][label] = dict(
                counts, nonpositive_fraction=fraction, valid_fraction=valid_fraction
            )
            if fraction > policy.max_nonpositive_fraction:
                report["reasons"].append(
                    f"{label}: non-positive power fraction {fraction:.6f} exceeds {policy.max_nonpositive_fraction}"
                )
            if valid_fraction < policy.min_valid_fraction:
                report["reasons"].append(f"{label}: insufficient valid observations")
            if counts["invalid_incidence_pixels"]:
                report["reasons"].append(f"{label}: invalid incidence angles")
        report["passed"] = not report["reasons"]
    except (ValueError, OSError, RasterioError, ET.ParseError) as exc:
        report["reasons"].append(str(exc))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    if not report["passed"]:
        raise ProcessingQualityError(report)
    return report
