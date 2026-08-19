"""Unit tests for ingest clients (CDSE, ASF, MPC) and local cache manager."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryolens.config.settings import CDSESettings
from cryolens.ingest.cache import LocalCacheManager
from cryolens.ingest.cdse import CDSEClient, SARSceneMetadata


def test_cache_manager_operations(tmp_path: Path) -> None:
    """Test LocalCacheManager caching, touch, byte tracking, and pinning."""
    cache = LocalCacheManager(cache_dir=tmp_path, max_size_bytes=1024 * 1024)
    file_path = cache.get_path("test_item_1", subdirectory="raw")
    assert file_path.parent.name == "raw"
    assert not cache.contains("test_item_1", subdirectory="raw")

    file_path.write_bytes(b"hello sar data" * 100)
    assert cache.contains("test_item_1", subdirectory="raw")

    cache.record_transfer(1400)
    assert cache.total_bytes_transferred == 1400

    cache.pin("test_item_1")
    assert "test_item_1" in cache._pinned_keys
    cache.unpin("test_item_1")
    assert "test_item_1" not in cache._pinned_keys


def test_sar_scene_metadata_model() -> None:
    """Verify SARSceneMetadata validation and properties."""
    meta = SARSceneMetadata(
        scene_id="S1B_EW_GRDM_1SDH_20200515T094821_021590_E720",
        platform="Sentinel-1B",
        instrument_mode="EW",
        polarizations=["HH", "HV"],
        product_type="GRD",
        acquisition_time=datetime(2020, 5, 15, 9, 48, 21, tzinfo=UTC),
        start_time=datetime(2020, 5, 15, 9, 48, 21, tzinfo=UTC),
        end_time=datetime(2020, 5, 15, 9, 49, 21, tzinfo=UTC),
        footprint_geojson={"type": "Polygon", "coordinates": []},
        orbit_direction="DESCENDING",
        download_url="https://dataspace.copernicus.eu/odata/v1/Products(1)/$value",
    )
    assert meta.scene_id.startswith("S1B_EW")
    assert "HH" in meta.polarizations
    assert meta.instrument_mode == "EW"


def test_cdse_client_feature_parsing() -> None:
    """Test CDSE STAC JSON feature parsing into SARSceneMetadata."""
    client = CDSEClient(cdse_settings=CDSESettings(username="mock_user", password="mock_password"))
    sample_feature: dict[str, Any] = {
        "id": "S1A_EW_GRDM_1SDH_20240401T100000_050000_01A2B3_1234",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[-55.0, 48.0], [-50.0, 48.0], [-50.0, 50.0], [-55.0, 50.0], [-55.0, 48.0]]
            ],
        },
        "properties": {
            "platform": "Sentinel-1A",
            "datetime": "2024-04-01T10:00:00Z",
            "sar:instrument_mode": "EW",
            "sar:polarizations": ["HH", "HV"],
            "sar:product_type": "GRD",
            "sat:orbit_state": "ascending",
            "sat:absolute_orbit": 50000,
        },
        "assets": {"PRODUCT": {"href": "https://dataspace.copernicus.eu/download/sample.zip"}},
    }

    metadata = client._parse_stac_feature(sample_feature)
    assert metadata is not None
    assert metadata.scene_id == "S1A_EW_GRDM_1SDH_20240401T100000_050000_01A2B3_1234"
    assert metadata.instrument_mode == "EW"
    assert metadata.polarizations == ["HH", "HV"]
    assert metadata.download_url == "https://dataspace.copernicus.eu/download/sample.zip"
