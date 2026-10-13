"""Receipt-gated SAFE pairs, unchanged quality limits and human evidence behavior."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from geoalchemy2.shape import from_shape
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import Point, box, mapping

from cryolens.api.security import require_analyst
from cryolens.eval.cohort import file_digest
from cryolens.preprocess.stack import BAND_NAMES
from cryolens.review.fresh import (
    ReviewLocation,
    checked_product,
    radar_window,
    select_items,
    select_locations,
)
from cryolens.review.fresh_app import create_app
from cryolens.review.pairs import build_pair
from cryolens.review.policy import PairingPolicy


@pytest.fixture
def corrected(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    p = tmp_path / "corrected.tif"
    x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(-53, 50)
    transform = from_origin(x - 4000, y + 4000, 40, 40)
    values = np.stack([np.full((200, 200), v, dtype="float32") for v in [-20, -30, 10, 35]])
    values[:, 0, 0] = -9999
    with rasterio.open(
        p,
        "w",
        driver="GTiff",
        height=200,
        width=200,
        count=4,
        dtype="float32",
        crs="EPSG:3978",
        transform=transform,
        nodata=-9999,
    ) as dst:
        dst.write(values)
        for i, name in enumerate(BAND_NAMES, 1):
            dst.set_band_description(i, name)
    report = json.loads(
        Path("docs/processing-validation/v5/report.json").read_text(encoding="utf-8")
    )
    product = report["products"][0]
    product["output_sha256"] = file_digest(p)
    product["output_quality"].update(
        output_sha256=file_digest(p), shape=[200, 200], transform=list(transform)
    )
    return p, report


def test_qualification_binds_actual_pixels_and_preserves_strict_limits(
    corrected: tuple[Path, dict[str, Any]],
) -> None:
    path, report = corrected
    name = report["products"][0]["product"]
    assert checked_product(report, name, path)["mode"] == "EW"
    altered = copy.deepcopy(report)
    altered["products"][0]["windows"][0]["checks"]["corrected_reprojection_HV"][
        "p95_absolute_error_db"
    ] = 1.01
    with pytest.raises(ValueError, match="fixed limits"):
        checked_product(altered, name, path)
    altered = copy.deepcopy(report)
    altered["policy"]["max_nonpositive_fraction"] = 0.06
    with pytest.raises(ValueError, match="policy"):
        checked_product(altered, name, path)
    with path.open("ab") as f:
        f.write(b"changed")
    with pytest.raises(ValueError, match="differs"):
        checked_product(report, name, path)


def test_radar_window_retains_corrected_grid_and_nodata(
    corrected: tuple[Path, dict[str, Any]],
) -> None:
    path, _ = corrected
    chip = radar_window(path, 0, 0, {}, radius=100)
    with rasterio.open(path) as src:
        assert chip.transform == src.transform
        assert np.isnan(chip.hh[0, 0])
        assert chip.hh[1, 1] == -20 and chip.hv[1, 1] == -30
    assert chip.source["review_only"] is True


class Water:
    def generate_land_mask(self, *args: Any, **kwargs: Any) -> np.ndarray:
        return np.zeros((1, 1), dtype=bool)


def test_selection_is_regular_not_brightness_ranked(corrected: tuple[Path, dict[str, Any]]) -> None:
    path, _ = corrected
    points = select_locations(path, Water())  # type: ignore[arg-type]
    assert len(points) == 25
    assert {p["row"] for p in points} == {20, 60, 100, 140, 180}
    assert all(p["included"] for p in points)
    with rasterio.open(path, "r+") as dst:
        dst.write(np.full((200, 200), 1, dtype="float32"), 1)
    assert select_locations(path, Water()) == points  # type: ignore[arg-type]


class OpticalFixture:
    provider = "planetary-computer"

    def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("No network in fixture")

    def download_windows(
        self, item: dict[str, Any], bounds: tuple[float, float, float, float], output: Path
    ) -> dict[str, Any]:
        assets = {}
        for band in ("B02", "B03", "B04", "B08", "SCL"):
            spacing = 20 if band == "SCL" else 10
            size = int(round((bounds[2] - bounds[0]) / spacing))
            path = output / f"{band}-native.tif"
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=size,
                width=size,
                count=1,
                dtype="uint16",
                crs="EPSG:3978",
                transform=from_origin(bounds[0], bounds[3], spacing, spacing),
                nodata=0,
            ) as dst:
                dst.write(np.full((size, size), 6 if band == "SCL" else 1000, dtype="uint16"), 1)
            assets[band] = {
                "filename": path.name,
                "sha256": file_digest(path),
                "native_pixel_spacing_m": spacing,
            }
        return assets


@pytest.fixture
def pair_root(tmp_path: Path, corrected: tuple[Path, dict[str, Any]]) -> Path:
    root = tmp_path / "run"
    root.mkdir()
    path, _ = corrected
    point = Point(-53, 50)
    subject = ReviewLocation(
        "test-location",
        from_shape(point, srid=4326),
        SimpleNamespace(
            acquisition_time=datetime(2026, 3, 25, tzinfo=UTC), product_id="fixture-source"
        ),
    )
    radar = radar_window(path, 100, 100, {"source_sha256": file_digest(path)})
    item = {
        "id": "fixture-optical",
        "geometry": mapping(box(-54, 49, -52, 51)),
        "properties": {"datetime": "2026-03-25T00:00:00Z"},
    }
    pair = build_pair(
        subject,
        radar,
        item,
        OpticalFixture(),
        root / "pairs",
        PairingPolicy(),
        {"items": [item], "complete": True},
    )
    assert pair["detection_id"] is None and pair["subject_kind"] == "review_location"
    with rasterio.open(root / "pairs" / pair["id"] / "radar-native.tif") as src:
        assert src.transform == radar.transform
        assert src.crs.to_epsg() == 3978
        assert src.read(1)[100, 100] == -20
    (root / "report.json").write_text(
        json.dumps(
            {
                "locations": [
                    {
                        "pairs": [
                            {
                                "id": pair["id"],
                                "manifest_sha256": file_digest(
                                    root / "pairs" / pair["id"] / "pair.json"
                                ),
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def test_authenticated_append_only_optical_evidence(pair_root: Path) -> None:
    app = create_app(pair_root)
    client = TestClient(app)
    identifier = json.loads((pair_root / "report.json").read_text())["locations"][0]["pairs"][0][
        "id"
    ]
    url = f"/api/pairs/{identifier}/evidence"
    body = {
        "assessment": "corroborating",
        "visibility": "clear",
        "sar_observed_lonlat": [-53, 50],
        "observed_lonlat": [-53, 50],
        "personally_inspected": True,
        "viewed_channels": ["HH", "HV"],
        "notes": "Test fixture optical observation only.",
    }
    assert client.post(url, json=body).status_code in (401, 503)
    app.dependency_overrides[require_analyst] = lambda: "fixture-reviewer"
    assert client.post(url, json={**body, "sar_observed_lonlat": None}).status_code == 422
    assert client.post(url, json={**body, "observed_lonlat": [-53, 50.01]}).status_code == 422
    saved = client.post(url, json=body)
    assert saved.status_code == 201
    assert saved.json()["radar_verdict_changed"] is False
    assert saved.json()["identity"] == "unknown"
    assert (
        client.post(
            url, json={**body, "assessment": "ambiguous", "visibility": "uncertain"}
        ).status_code
        == 201
    )
    assert len(client.get(f"/api/pairs/{identifier}").json()["evidence_history"]) == 2
    assert client.delete(url).status_code == 405
    file = pair_root / "pairs" / identifier / "paired.png"
    file.write_bytes(b"changed")
    assert client.get(f"/api/pairs/{identifier}").status_code == 409


def test_absence_requires_full_coverage(pair_root: Path) -> None:
    app = create_app(pair_root)
    app.dependency_overrides[require_analyst] = lambda: "fixture"
    client = TestClient(app)
    identifier = json.loads((pair_root / "report.json").read_text())["locations"][0]["pairs"][0][
        "id"
    ]
    body = {
        "assessment": "no_visible_counterpart",
        "visibility": "clear",
        "sar_observed_lonlat": [-53, 50.001],
        "personally_inspected": True,
        "viewed_channels": ["HH", "HV"],
        "notes": "The observed point is offset from the screening centre.",
    }
    assert client.post(f"/api/pairs/{identifier}/evidence", json=body).status_code == 422


def test_optical_selection_reports_truncation() -> None:
    items = [
        {
            "id": str(i),
            "properties": {"datetime": "2026-03-25T00:00:00Z"},
            "geometry": mapping(box(-54, 49, -52, 51)),
        }
        for i in range(4)
    ]
    selected, total = select_items(
        {"items": items}, Point(-53, 50), datetime(2026, 3, 25, tzinfo=UTC), PairingPolicy()
    )
    assert total == 4 and len(selected) == 3


def test_direct_verification_refuses_replacing_scientific_history(
    pair_root: Path, tmp_path: Path
) -> None:
    from cryolens.review.verify_optical import verify_reference

    identifier = json.loads((pair_root / "report.json").read_text())["locations"][0]["pairs"][0][
        "id"
    ]
    artifacts = pair_root / "pairs" / identifier
    output = tmp_path / "existing-verification"
    output.mkdir()
    receipt = output / "comparison.json"
    receipt.write_text("preserved scientific history")
    with pytest.raises(FileExistsError):
        verify_reference(artifacts / "pair.json", output, artifacts=artifacts)
    assert receipt.read_text() == "preserved scientific history"


def test_direct_verification_refuses_wrong_native_directory(
    pair_root: Path, tmp_path: Path
) -> None:
    from cryolens.review.verify_optical import verify_reference

    identifier = json.loads((pair_root / "report.json").read_text())["locations"][0]["pairs"][0][
        "id"
    ]
    artifacts = pair_root / "pairs" / identifier
    with pytest.raises(ValueError, match="differs"):
        verify_reference(artifacts / "pair.json", tmp_path / "new-verification", artifacts=tmp_path)
    assert not (tmp_path / "new-verification").exists()


def test_observed_optical_position_respects_cloud_buffer(pair_root: Path) -> None:
    from cryolens.review.fresh_app import valid_point

    identifier = json.loads((pair_root / "report.json").read_text())["locations"][0]["pairs"][0][
        "id"
    ]
    path = pair_root / "pairs" / identifier / "optical-aligned.tif"
    with rasterio.open(path, "r+") as raster:
        x, y = Transformer.from_crs(4326, raster.crs, always_xy=True).transform(-53, 50)
        row, col = raster.index(x, y)
        from rasterio.windows import Window

        raster.write(np.array([[9]], dtype="float32"), 5, window=Window(col + 3, row, 1, 1))
    with pytest.raises(ValueError, match="cloud/shadow buffer"):
        valid_point(path, (-53, 50), [1, 2, 3, 4, 5], 60)


def alter_optical_quality(path: Path, case: str) -> tuple[float, float]:
    from rasterio.windows import Window

    with rasterio.open(path, "r+") as src:
        row, col = src.height // 2, src.width // 2
        if case == "stale_absence":
            src.write(np.full((src.height, src.width), 9, dtype="float32"), 5)
        elif case == "edge":
            col = 1
        elif case == "cloud":
            src.write(np.array([[9]], dtype="float32"), 5, window=Window(col, row, 1, 1))
        elif case == "ambiguous":
            src.write(np.array([[7]], dtype="float32"), 5, window=Window(col, row, 1, 1))
        elif case in {"near_cloud", "quality_gap", "quality_nan", "unknown_class"}:
            value = {"near_cloud": 9, "quality_gap": 0, "quality_nan": np.nan, "unknown_class": 12}[
                case
            ]
            src.write(np.array([[value]], dtype="float32"), 5, window=Window(col + 3, row, 1, 1))
        elif case == "mask_gap":
            mask = np.full((src.height, src.width), 255, dtype="uint8")
            mask[row, col + 3] = 0
            src.write_mask(mask)
        x, y = src.xy(row, col)
        lon, lat = Transformer.from_crs(src.crs, 4326, always_xy=True).transform(x, y)
        return lon, lat


@pytest.mark.parametrize(
    "case",
    [
        "clear",
        "cloud",
        "ambiguous",
        "near_cloud",
        "edge",
        "quality_gap",
        "quality_nan",
        "unknown_class",
        "mask_gap",
        "stale_absence",
    ],
)
def test_fresh_endpoint_requires_complete_clear_buffer(pair_root: Path, case: str) -> None:
    report_path = pair_root / "report.json"
    report = json.loads(report_path.read_text())
    reference = report["locations"][0]["pairs"][0]
    folder = pair_root / "pairs" / reference["id"]
    position = alter_optical_quality(folder / "optical-aligned.tif", case)
    manifest_path = folder / "pair.json"
    pair = json.loads(manifest_path.read_text())
    pair["files"] = {name: file_digest(folder / name) for name in pair["files"]}
    manifest_path.write_text(json.dumps(pair))
    reference["manifest_sha256"] = file_digest(manifest_path)
    report_path.write_text(json.dumps(report))
    app = create_app(pair_root)
    app.dependency_overrides[require_analyst] = lambda: "fixture"
    client = TestClient(app)
    response = client.post(
        f"/api/pairs/{reference['id']}/evidence",
        json={
            "assessment": "no_visible_counterpart" if case == "stale_absence" else "corroborating",
            "visibility": "clear",
            "sar_observed_lonlat": [-53, 50],
            "observed_lonlat": position,
            "personally_inspected": True,
            "viewed_channels": ["HH", "HV"],
            "notes": "Synthetic buffer regression evidence only.",
        },
    )
    assert response.status_code == (201 if case == "clear" else 422), response.text
    if case != "clear":
        assert ("screened coverage" if case == "stale_absence" else "buffer") in response.text
    history = client.get(f"/api/pairs/{reference['id']}").json()["evidence_history"]
    assert len(history) == (1 if case == "clear" else 0)


def test_strict_rescreen_is_additive_and_reserves_output(pair_root: Path, tmp_path: Path) -> None:
    from cryolens.review.strict_screening import rescreen

    report = pair_root / "report.json"
    data = json.loads(report.read_text())
    data["denominator"] = "synthetic predeclared review locations"
    data["locations"][0]["id"] = "synthetic"
    data["locations"][0]["mode"] = "IW"
    reference = data["locations"][0]["pairs"][0]
    folder = pair_root / "pairs" / reference["id"]
    manifest = json.loads((folder / "pair.json").read_text())
    reference.update(optical_item_id=manifest["optical_item_id"])
    report.write_text(json.dumps(data))
    before = {p: file_digest(p) for p in pair_root.rglob("*") if p.is_file()}
    output = tmp_path / "strict-screening"
    result = rescreen(pair_root, output)
    assert result["counts"]["IW"]["strict_useful"] == 1
    assert result["locations"][0]["pairs"][0]["manifest_sha256"] == file_digest(
        folder / "pair.json"
    )
    assert result["executed_code_sha256"] == file_digest(output / "executed-code.zip")
    assert before == {p: file_digest(p) for p in before}
    with pytest.raises(FileExistsError):
        rescreen(pair_root, output)
