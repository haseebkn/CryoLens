import json
from pathlib import Path

from cryolens.config.settings import (
    AppConfig,
    ProjectConfig,
    Settings,
    get_app_config,
    get_project_config,
    get_settings,
)


def test_default_settings() -> None:
    """Verify default settings initialization."""
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.env in ["development", "testing", "production"]
    assert settings.db.port == 5432
    assert settings.db.db == "cryolens"
    assert "postgresql://" in settings.db.url
    assert "postgresql+asyncpg://" in settings.db.async_url
    assert settings.mlflow.experiment_name == "cryolens-benchmarks"


def test_settings_with_env_override(mock_env_vars: None) -> None:
    """Verify settings parse environment variable overrides accurately."""
    settings = Settings()
    assert settings.env == "testing"
    assert settings.log_level == "DEBUG"
    assert settings.db.host == "test-db-host"
    assert settings.db.port == 5433
    assert settings.db.user == "test_user"
    assert settings.db.password == "secret123"
    assert settings.db.url == "postgresql://test_user:secret123@test-db-host:5433/cryolens_test"

    assert settings.cdse.username == "test_cdse_user"
    assert settings.cdse.password == "test_cdse_pass"
    assert settings.cdse.has_credentials is True

    assert settings.earthdata.username == "test_ed_user"
    assert settings.earthdata.password == "test_ed_pass"
    assert settings.earthdata.has_credentials is True

    assert settings.kaggle.username == "test_kaggle_user"
    assert settings.kaggle.key == "test_kaggle_key"
    assert settings.kaggle.has_credentials is True


def test_project_yaml_loading(sample_project_yaml_path: Path) -> None:
    """Verify project.yaml parses into typed ProjectConfig model."""
    config = get_project_config(str(sample_project_yaml_path))

    assert config.project.name == "CryoLens"
    assert config.spatial.target_crs == "EPSG:3978"
    assert config.spatial.source_crs == "EPSG:4326"
    assert config.spatial.pixel_spacing_m == 40.0
    assert config.spatial.bbox.west == -60.0
    assert config.spatial.bbox.east == -46.0
    assert config.spatial.bbox.south == 43.5
    assert config.spatial.bbox.north == 55.0

    assert 4 in config.season.peak_months
    assert config.tiling.tile_size_px == 256
    assert config.tiling.min_object_dim_px == 2

    assert "iceberg" in config.taxonomy.classes
    assert "ship" in config.taxonomy.classes

    growler = next(c for c in config.taxonomy.iip_size_classes if c.name == "Growler")
    assert growler.detectable_ew_grd is False

    large = next(c for c in config.taxonomy.iip_size_classes if c.name == "Large")
    assert large.detectable_ew_grd is True

    assert config.endpoints.cdse.stac_url == "https://stac.dataspace.copernicus.eu/v1"
    assert config.endpoints.cdse.instrument_mode == "EW"
    assert "HH" in config.endpoints.cdse.polarizations
    assert config.cfar.distribution == "k_distribution"


def test_real_project_yaml_file_exists() -> None:
    """Ensure the actual repository configs/project.yaml is valid and loads."""
    config = get_project_config("configs/project.yaml")
    assert config.project.name == "CryoLens"
    assert config.spatial.target_crs == "EPSG:3978"
    assert Path(config.spatial.aoi_file).is_file()


def test_real_aoi_geojson_file_valid() -> None:
    """Ensure configs/aoi.geojson is valid GeoJSON over Newfoundland & Labrador.

    The AOI carries the full marine area plus the Labrador Shelf and Grand Banks
    sub-regions, so metrics can be reported for the northern transit corridor and
    the southern production area separately.
    """
    aoi_path = Path("configs/aoi.geojson")
    assert aoi_path.is_file()

    with open(aoi_path, encoding="utf-8") as f:
        data = json.load(f)

    assert data["type"] == "FeatureCollection"
    features = data["features"]
    assert len(features) >= 1

    ids = {f["properties"]["id"] for f in features}
    assert "newfoundland_labrador_marine" in ids

    for feature in features:
        coords = feature["geometry"]["coordinates"][0]
        assert len(coords) == 5, "each AOI polygon is a closed rectangle"
        assert coords[0] == coords[-1], "polygon must close"

    primary = next(f for f in features if f["properties"]["id"] == "newfoundland_labrador_marine")
    west, south, east, north = primary["properties"]["bbox"]
    # Cape Chidley is the northern tip of Labrador; the Tail of the Grand Bank
    # is the southern limit of the area CryoLens covers.
    assert north >= 60.0, "AOI must reach the northern tip of Labrador"
    assert south <= 43.0, "AOI must reach the Tail of the Grand Bank"
    assert west <= -60.0 and east >= -46.0


def test_get_app_config() -> None:
    """Test combined AppConfig loader."""
    app_config = get_app_config()
    assert isinstance(app_config, AppConfig)
    assert isinstance(app_config.settings, Settings)
    assert isinstance(app_config.project, ProjectConfig)
