"""Optical absence, quality, provenance and authenticated evidence regressions."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from pyproj import Transformer
from rasterio.transform import from_origin
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cryolens.config.settings import get_settings
from cryolens.db.models import (
    DetectionModel,
    OpticalEvidenceModel,
    OpticalPairModel,
    ValidationModel,
)
from cryolens.eval.cohort import digest, file_digest
from cryolens.ingest.sentinel2 import BANDS, Sentinel2Client, utc
from cryolens.review.pairs import assess_visibility
from cryolens.review.policy import PairingPolicy
from tests.unit.test_api import KEY, first_id
from tests.unit.test_api import api_database as api_database
from tests.unit.test_api import api_test_client as api_test_client


def test_visibility_distinguishes_clouds_shadows_invalid_and_ice() -> None:
    policy = PairingPolicy(cloud_buffer_m=0)
    valid = np.ones((20, 20), dtype=bool)
    for code in (0, 1, 2, 3, 7, 8, 9, 10):
        result = assess_visibility(np.full(valid.shape, code), valid, valid, policy)
        assert result["useful_for_review"] is False
    ice = assess_visibility(np.full(valid.shape, 11), valid, valid, policy)
    assert ice["useful_for_review"] is True
    assert ice["snow_ice_fraction"] == 1
    assert ice["analyst_inspection_required"] is True
    nodata = assess_visibility(np.full(valid.shape, 6), ~valid, valid, policy)
    assert nodata["status"] == "unavailable"
    with pytest.raises(ValueError, match="nonempty"):
        assess_visibility(np.full(valid.shape, 6), valid, ~valid, policy)


def test_cloud_buffer_reaches_diagonal_neighbours() -> None:
    scl = np.full((21, 21), 6)
    scl[10, 10] = 9
    domain = np.zeros_like(scl, dtype=bool)
    domain[14, 14] = True  # 56.6 m from the cloud at a 10 m review grid.
    result = assess_visibility(scl, np.ones_like(domain), domain, PairingPolicy())
    assert result["cloud_or_shadow_buffered_fraction"] == 1
    assert result["visible_fraction"] == 0


def test_movement_envelope_accounts_for_both_time_directions() -> None:
    policy = PairingPolicy()
    assert policy.matching_radius(3600, 80) == policy.matching_radius(-3600, 80) == 1960
    with pytest.raises(ValueError, match="timezone"):
        utc(datetime(2020, 1, 1))
    with pytest.raises(ValueError, match="Minimum"):
        PairingPolicy(minimum_review_radius_m=7000)
    with pytest.raises(ValueError):
        PairingPolicy(sar_geolocation_allowance_m=float("inf"))


def test_public_search_paginates_and_keeps_cloudy_tiles_for_local_review() -> None:
    class Response:
        def __init__(self, page: dict[str, Any]) -> None:
            self.page = page

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return self.page

    class SessionStub:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def request(self, method: str, url: str, **kwargs: Any) -> Response:
            self.calls.append(kwargs)
            feature = {
                "id": str(len(self.calls)),
                "collection": "sentinel-2-l2a",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[-53, 47], [-52, 47], [-52, 48], [-53, 48], [-53, 47]]],
                },
                "properties": {"datetime": "2020-05-15T12:00:00Z", "eo:cloud_cover": 99},
                "assets": {},
            }
            return Response(
                {
                    "features": [feature],
                    "links": [
                        {
                            "rel": "next",
                            "href": "https://planetarycomputer.microsoft.com/api/stac/v1/search?token=next",
                        }
                    ]
                    if len(self.calls) == 1
                    else [],
                }
            )

    session = SessionStub()
    from typing import cast

    import requests

    client = Sentinel2Client(cast(requests.Session, session))
    result = client.search(
        {
            "type": "Polygon",
            "coordinates": [[[-53, 47], [-52, 47], [-52, 48], [-53, 48], [-53, 47]]],
        },
        datetime(2020, 5, 15, 10, tzinfo=UTC),
    )
    assert result["complete"] is True and len(result["items"]) == 2
    assert "eo:cloud_cover" not in session.calls[0]["json"].get("query", {})
    assert result["items"][0]["properties"]["eo:cloud_cover"] == 99


def test_native_download_preserves_source_pixels_grid_and_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryolens.ingest import sentinel2

    x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(-52.5, 47.5)
    item: dict[str, Any] = {"id": "synthetic-test-only", "assets": {}}
    sources = {}
    for band in BANDS:
        spacing = 20 if band == "SCL" else 10
        size = 200 // spacing
        source = tmp_path / f"source-{band}.tif"
        values = np.arange(size * size, dtype=np.uint16).reshape(size, size) + 1
        with rasterio.open(
            source,
            "w",
            driver="GTiff",
            width=size,
            height=size,
            count=1,
            dtype="uint16",
            crs="EPSG:3978",
            transform=from_origin(x - 100, y + 100, spacing, spacing),
            nodata=0,
        ) as dst:
            dst.write(values, 1)
        href = f"https://test.blob.core.windows.net/{band}.tif"
        sources[href] = str(source)
        item["assets"][band] = {"href": href}
    monkeypatch.setattr(sentinel2.planetary_computer, "sign_url", lambda href: sources[href])
    receipt = Sentinel2Client().download_windows(
        item, (x - 100, y - 100, x + 100, y + 100), tmp_path / "native"
    )
    for band in BANDS:
        with (
            rasterio.open(sources[item["assets"][band]["href"]]) as source,
            rasterio.open(tmp_path / "native" / receipt[band]["filename"]) as native,
        ):
            np.testing.assert_array_equal(native.read(1), source.read(1))
            assert native.transform == source.transform
            assert native.crs == source.crs
        assert receipt[band]["source_resampling"] == "none"
        assert (
            file_digest(tmp_path / "native" / receipt[band]["filename"]) == receipt[band]["sha256"]
        )


def pair_fixture(
    client: TestClient,
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    optical: bool = True,
) -> tuple[str, str, Path]:
    monkeypatch.setenv("CRYOLENS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    detection = first_id(client)
    pair_id = digest({"fixture": detection, "optical": optical})
    folder = tmp_path / "processed/optical-review" / pair_id
    folder.mkdir(parents=True)
    (folder / "paired.png").write_bytes(b"unit-test-image-fixture")
    x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(-52.5, 47.5)
    transform = from_origin(x - 100, y + 100, 10, 10)
    with rasterio.open(
        folder / "optical-aligned.tif",
        "w",
        driver="GTiff",
        width=20,
        height=20,
        count=5,
        dtype="float32",
        crs="EPSG:3978",
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(np.full((5, 20, 20), 6, dtype=np.float32))
    metadata = {
        "id": pair_id,
        "detection_id": detection,
        "optical_item_id": "unit-test-item" if optical else None,
        "candidate_lonlat": [-52.5, 47.5],
        "matching_radius_m": 80,
        "full_movement_envelope_in_chip": True,
        "visibility": {"useful_for_review": True},
        "alignment": {"transform": list(transform), "shape": [20, 20]},
        "files": {p.name: file_digest(p) for p in folder.iterdir()},
    }
    (folder / "pair.json").write_text(json.dumps(metadata))
    with factory() as session:
        session.add(
            OpticalPairModel(
                id=pair_id,
                detection_id=detection,
                manifest_sha256=file_digest(folder / "pair.json"),
                metadata_json=metadata,
            )
        )
        session.commit()
    return detection, pair_id, folder


def test_optical_evidence_is_authenticated_append_only_and_never_a_radar_verdict(
    api_test_client: TestClient,
    api_database: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detection, pair_id, _ = pair_fixture(api_test_client, api_database, tmp_path, monkeypatch)
    url = f"/api/v1/detections/{detection}/optical-pairs/{pair_id}/evidence"
    body = {
        "assessment": "ambiguous",
        "visibility": "uncertain",
        "notes": "Movement and cloud ambiguity require another source.",
    }
    assert api_test_client.post(url, json=body).status_code == 401
    for assessment, visibility in (("ambiguous", "uncertain"), ("no_visible_counterpart", "clear")):
        response = api_test_client.post(
            url,
            headers={"X-Analyst-Key": KEY},
            json={**body, "assessment": assessment, "visibility": visibility},
        )
        assert response.status_code == 201, response.text
        assert response.json()["radar_verdict_changed"] is False
    pairs = api_test_client.get(f"/api/v1/detections/{detection}/optical-pairs").json()
    assert len(pairs["pairs"][0]["evidence_history"]) == 2
    assert pairs["missing_optical_is_false_target"] is False
    with api_database() as session:
        assert session.scalar(select(func.count()).select_from(OpticalEvidenceModel)) == 2
        assert session.scalar(select(func.count()).select_from(ValidationModel)) == 0
        target = session.get(DetectionModel, detection)
        assert target is not None and target.predicted_class == "iceberg"  # raw fixture preserved


def test_missing_or_misaligned_optical_cannot_be_corroboration(
    api_test_client: TestClient,
    api_database: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detection, pair_id, _ = pair_fixture(
        api_test_client, api_database, tmp_path, monkeypatch, optical=False
    )
    url = f"/api/v1/detections/{detection}/optical-pairs/{pair_id}/evidence"
    body = {
        "assessment": "corroborating",
        "visibility": "clear",
        "observed_lonlat": [-52.5, 47.5],
        "notes": "Test supporting evidence with a source.",
    }
    assert api_test_client.post(url, headers={"X-Analyst-Key": KEY}, json=body).status_code == 422
    assert (
        api_test_client.post(
            url,
            headers={"X-Analyst-Key": KEY},
            json={**body, "assessment": "unavailable", "visibility": "unavailable"},
        ).status_code
        == 201
    )
    detection, pair_id, folder = pair_fixture(
        api_test_client, api_database, tmp_path, monkeypatch, optical=True
    )
    url = f"/api/v1/detections/{detection}/optical-pairs/{pair_id}/evidence"
    assert (
        api_test_client.post(
            url, headers={"X-Analyst-Key": KEY}, json={**body, "observed_lonlat": [-53, 48]}
        ).status_code
        == 422
    )
    assert api_test_client.post(url, headers={"X-Analyst-Key": KEY}, json=body).status_code == 201
    # Positive DN values alone cannot override a missing source-data mask.
    with rasterio.open(folder / "optical-aligned.tif", "r+") as raster:
        raster.write_mask(np.zeros((20, 20), dtype=np.uint8))
    metadata = json.loads((folder / "pair.json").read_text())
    metadata["files"] = {p.name: file_digest(p) for p in folder.iterdir() if p.name != "pair.json"}
    (folder / "pair.json").write_text(json.dumps(metadata))
    with api_database() as session:
        pair = session.get(OpticalPairModel, pair_id)
        assert pair is not None
        pair.manifest_sha256 = file_digest(folder / "pair.json")
        pair.metadata_json = metadata
        session.commit()
    response = api_test_client.post(url, headers={"X-Analyst-Key": KEY}, json=body)
    assert response.status_code == 422 and "valid observed pixels" in response.text
    with api_database() as session:
        pair = session.get(OpticalPairModel, pair_id)
        assert pair is not None
        pair.metadata_json = {**metadata, "full_movement_envelope_in_chip": False}
        session.commit()
    response = api_test_client.post(
        url,
        headers={"X-Analyst-Key": KEY},
        json={**body, "assessment": "no_visible_counterpart"},
    )
    assert response.status_code == 422 and "Incomplete displacement coverage" in response.text


def test_changed_pair_artifacts_are_not_served_or_reviewed(
    api_test_client: TestClient,
    api_database: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detection, pair_id, folder = pair_fixture(api_test_client, api_database, tmp_path, monkeypatch)
    url = f"/api/v1/detections/{detection}/optical-pairs/{pair_id}"
    assert api_test_client.get(url + "/assets/paired.png").status_code == 200
    (folder / "paired.png").write_bytes(b"changed")
    assert api_test_client.get(url + "/assets/paired.png").status_code == 409
    assert (
        api_test_client.post(
            url + "/evidence",
            headers={"X-Analyst-Key": KEY},
            json={
                "assessment": "ambiguous",
                "visibility": "uncertain",
                "notes": "A stale image must not be accepted.",
            },
        ).status_code
        == 409
    )
