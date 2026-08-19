"""Global pytest fixtures for CryoLens."""

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def sample_aoi_geojson_path(tmp_path: Path) -> Path:
    """Fixture returning a temporary valid AOI GeoJSON file."""
    aoi_data: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "id": "test_aoi",
                    "name": "Test AOI",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-60.0, 43.5],
                            [-46.0, 43.5],
                            [-46.0, 55.0],
                            [-60.0, 55.0],
                            [-60.0, 43.5],
                        ]
                    ],
                },
            }
        ],
    }
    file_path = tmp_path / "test_aoi.geojson"
    file_path.write_text(json.dumps(aoi_data), encoding="utf-8")
    return file_path


@pytest.fixture
def sample_project_yaml_path(tmp_path: Path, sample_aoi_geojson_path: Path) -> Path:
    """Fixture returning a temporary valid project.yaml configuration file."""
    config_dict: dict[str, Any] = {
        "project": {
            "name": "CryoLens",
            "description": "Test setup",
            "version": "0.1.0",
        },
        "spatial": {
            "target_crs": "EPSG:3978",
            "source_crs": "EPSG:4326",
            "pixel_spacing_m": 40.0,
            "effective_resolution_m": 90.0,
            "aoi_file": str(sample_aoi_geojson_path),
            "bbox": {
                "west": -60.0,
                "south": 43.5,
                "east": -46.0,
                "north": 55.0,
            },
        },
        "season": {
            "peak_months": [4, 5, 6],
            "extended_months": [2, 3, 4, 5, 6, 7],
        },
        "tiling": {
            "tile_size_px": 256,
            "tile_overlap_px": 32,
            "min_object_dim_px": 2,
        },
        "taxonomy": {
            "classes": ["iceberg", "ship", "offshore_structure", "sea_ice_feature", "clutter"],
            "iip_size_classes": [
                {
                    "name": "Growler",
                    "length_m": [0, 5],
                    "height_m": [0, 1],
                    "detectable_ew_grd": False,
                },
                {
                    "name": "Bergy Bit",
                    "length_m": [5, 15],
                    "height_m": [1, 5],
                    "detectable_ew_grd": False,
                },
                {
                    "name": "Small",
                    "length_m": [15, 60],
                    "height_m": [5, 15],
                    "detectable_ew_grd": True,
                },
                {
                    "name": "Medium",
                    "length_m": [60, 120],
                    "height_m": [15, 45],
                    "detectable_ew_grd": True,
                },
                {
                    "name": "Large",
                    "length_m": [120, 200],
                    "height_m": [45, 75],
                    "detectable_ew_grd": True,
                },
                {
                    "name": "Very Large",
                    "length_m": [200, 1000],
                    "height_m": [75, 500],
                    "detectable_ew_grd": True,
                },
            ],
        },
        "endpoints": {
            "cdse": {
                "stac_url": "https://stac.dataspace.copernicus.eu/v1",
                "token_url": "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
                "odata_url": "https://catalogue.dataspace.copernicus.eu/odata/v1",
                "collection": "SENTINEL-1",
                "instrument_mode": "EW",
                "polarizations": ["HH", "HV"],
                "product_type": "GRD",
            },
            "mpc": {
                "stac_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
                "collection": "sentinel-1-grd",
            },
            "cmems": {
                "live_product_id": "GLOBAL_ANALYSISFORECAST_PHY_001_024",
                "hindcast_product_id": "GLOBAL_MULTIYEAR_PHY_001_030",
            },
        },
        "cfar": {
            "default_pfa": 1.0e-5,
            "guard_window": [3, 3],
            "background_window": [15, 15],
            "distribution": "k_distribution",
        },
    }
    file_path = tmp_path / "project.yaml"
    file_path.write_text(yaml.dump(config_dict), encoding="utf-8")
    return file_path


@pytest.fixture
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Provide isolated environment variables for tests."""
    monkeypatch.setenv("CRYOLENS_ENV", "testing")
    monkeypatch.setenv("CRYOLENS_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("POSTGRES_HOST", "test-db-host")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "cryolens_test")
    monkeypatch.setenv("POSTGRES_USER", "test_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret123")
    monkeypatch.setenv("CDSE_USERNAME", "test_cdse_user")
    monkeypatch.setenv("CDSE_PASSWORD", "test_cdse_pass")
    monkeypatch.setenv("EARTHDATA_USERNAME", "test_ed_user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "test_ed_pass")
    monkeypatch.setenv("KAGGLE_USERNAME", "test_kaggle_user")
    monkeypatch.setenv("KAGGLE_KEY", "test_kaggle_key")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "./test_mlruns")
    monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", "test-experiment")
    yield
