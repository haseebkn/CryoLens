"""Corrected-output comparisons enforce the frozen numeric and grid limits."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.stack import COGStackBuilder
from validation.validate_complete_products import compare_complete_window


class ReferenceReader:
    def __init__(self, powers: np.ndarray) -> None:
        self.powers = powers

    def read_sigma0(
        self, pol: str, remove_thermal_noise: bool = True, window: Any = None
    ) -> dict[str, Any]:
        index = 0 if pol == "HH" else 1
        return {
            "sigma0_linear": self.powers[index]
            if remove_thermal_noise
            else self.powers[index] + 0.0001,
            "latitude": np.full((512, 512), 48.0),
            "longitude": np.full((512, 512), -53.0),
            "incidence_angle_deg": np.full((512, 512), 35.0),
        }


@pytest.fixture
def comparison_case(tmp_path: Path) -> tuple[ReferenceReader, Path, Path, dict[str, np.ndarray]]:
    rng = np.random.default_rng(29)
    powers = np.exp(rng.normal(-5, 0.5, size=(2, 512, 512))).astype(np.float32)
    reader = ReferenceReader(powers)
    prefix = tmp_path / "reference"
    for stage, names in [
        ("raw", ["Sigma0_HH", "Sigma0_HV"]),
        ("den", ["sigma0_hh", "sigma0_hv", "latitude_band", "longitude_band", "incidence"]),
    ]:
        folder = Path(str(prefix) + "_" + stage + ".data")
        folder.mkdir()
        for name in names:
            if name.startswith(("Sigma0_", "sigma0_")):
                values = reader.read_sigma0(name[-2:].upper(), stage == "den")["sigma0_linear"]
            else:
                values = np.full(
                    (512, 512),
                    {"latitude_band": 48.0, "longitude_band": -53.0, "incidence": 35.0}[name],
                    dtype=np.float32,
                )
            with rasterio.open(
                folder / (name + ".img"),
                "w",
                driver="ENVI",
                width=512,
                height=512,
                count=1,
                dtype="float32",
            ) as dst:
                dst.write(values.astype(np.float32), 1)
    transform = from_origin(100000, 500000, 40, 40)
    with rasterio.open(
        Path(str(prefix) + "_geo.tif"),
        "w",
        driver="GTiff",
        height=512,
        width=512,
        count=2,
        dtype="float32",
        crs=3978,
        transform=transform,
    ) as dst:
        dst.write(powers)
    Path(str(prefix) + "-receipt.json").write_text("{}")
    hh = 10 * np.log10(powers[0])
    hv = 10 * np.log10(powers[1])
    bands = {
        "sigma0_hh_db": hh,
        "sigma0_hv_db": hv,
        "ratio_hh_hv": hh - hv,
        "incidence_angle": np.full((512, 512), 35.0, dtype=np.float32),
    }
    output = COGStackBuilder(tmp_path / "output").build_and_export_cog(
        "fixture", bands, transform, provenance={"source_kind": "synthetic"}
    )
    return reader, prefix, output, bands


def test_actual_complete_pixels_pass_matched_reference(
    comparison_case: tuple[ReferenceReader, Path, Path, dict[str, np.ndarray]],
) -> None:
    reader, prefix, output, _ = comparison_case
    report = compare_complete_window(
        output, cast(SAFEProductReader, reader), (0, 0, 512, 512), prefix
    )
    assert report["scientific_comparison_passed"] is True
    assert report["checks"]["corrected_reprojection_HV"]["matched_pixels"] == 508 * 508
    assert report["checks"]["corrected_reprojection_HV"]["p95_absolute_error_db"] < 0.0001


def test_planted_radiometric_error_cannot_pass(
    comparison_case: tuple[ReferenceReader, Path, Path, dict[str, np.ndarray]], tmp_path: Path
) -> None:
    reader, prefix, _, bands = comparison_case
    bands["sigma0_hv_db"] += 1.1
    bands["ratio_hh_hv"] = bands["sigma0_hh_db"] - bands["sigma0_hv_db"]
    output = COGStackBuilder(tmp_path / "wrong").build_and_export_cog(
        "fixture",
        bands,
        from_origin(100000, 500000, 40, 40),
        provenance={"source_kind": "synthetic"},
    )
    report = compare_complete_window(
        output, cast(SAFEProductReader, reader), (0, 0, 512, 512), prefix
    )
    assert report["scientific_comparison_passed"] is False
    assert report["checks"]["corrected_reprojection_HV"]["passed"] is False


def test_planted_spatial_shift_cannot_pass(
    comparison_case: tuple[ReferenceReader, Path, Path, dict[str, np.ndarray]], tmp_path: Path
) -> None:
    reader, prefix, _, bands = comparison_case
    bands["sigma0_hv_db"] = np.roll(bands["sigma0_hv_db"], 3, axis=0)
    bands["ratio_hh_hv"] = bands["sigma0_hh_db"] - bands["sigma0_hv_db"]
    output = COGStackBuilder(tmp_path / "shifted").build_and_export_cog(
        "fixture",
        bands,
        from_origin(100000, 500000, 40, 40),
        provenance={"source_kind": "synthetic"},
    )
    report = compare_complete_window(
        output, cast(SAFEProductReader, reader), (0, 0, 512, 512), prefix
    )
    assert report["checks"]["registration_HV"]["passed"] is False
    assert report["checks"]["registration_HV"]["relative_displacement_m"] > 40


def test_unaligned_grid_cannot_be_claimed_matched(
    comparison_case: tuple[ReferenceReader, Path, Path, dict[str, np.ndarray]], tmp_path: Path
) -> None:
    reader, prefix, _, bands = comparison_case
    output = COGStackBuilder(tmp_path / "unaligned").build_and_export_cog(
        "fixture",
        bands,
        from_origin(100001, 500000, 40, 40),
        provenance={"source_kind": "synthetic"},
    )
    with pytest.raises(ValueError, match="aligned"):
        compare_complete_window(output, cast(SAFEProductReader, reader), (0, 0, 512, 512), prefix)
