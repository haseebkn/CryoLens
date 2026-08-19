"""Type-safe configuration and credential management for CryoLens."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    """PostgreSQL + PostGIS connection parameters."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    db: str = Field(default="cryolens", validation_alias="POSTGRES_DB")
    user: str = Field(default="cryolens_user", validation_alias="POSTGRES_USER")
    password: str = Field(default="", validation_alias="POSTGRES_PASSWORD")

    @property
    def url(self) -> str:
        """Construct standard SQLAlchemy connection URI."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"

    @property
    def async_url(self) -> str:
        """Construct async asyncpg connection URI."""
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.db}"


class CDSESettings(BaseModel):
    """Copernicus Data Space Ecosystem (CDSE) credentials."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    username: str = Field(default="", validation_alias="CDSE_USERNAME")
    password: str = Field(default="", validation_alias="CDSE_PASSWORD")
    client_id: str = Field(default="", validation_alias="CDSE_CLIENT_ID")
    client_secret: str = Field(default="", validation_alias="CDSE_CLIENT_SECRET")

    @property
    def has_credentials(self) -> bool:
        """Check if any valid authentication credential pair is configured."""
        has_basic = bool(self.username and self.password)
        has_oauth = bool(self.client_id and self.client_secret)
        return has_basic or has_oauth


class EarthdataSettings(BaseModel):
    """NASA Earthdata credentials (ASF DAAC / NSIDC)."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    username: str = Field(default="", validation_alias="EARTHDATA_USERNAME")
    password: str = Field(default="", validation_alias="EARTHDATA_PASSWORD")

    @property
    def has_credentials(self) -> bool:
        """Check if Earthdata credentials are provided."""
        return bool(self.username and self.password)


class PlanetaryComputerSettings(BaseModel):
    """Microsoft Planetary Computer configuration."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    subscription_key: str = Field(default="", validation_alias="PC_SDK_SUBSCRIPTION_KEY")


class CopernicusMarineSettings(BaseModel):
    """Copernicus Marine Service (CMEMS) credentials."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    username: str = Field(default="", validation_alias="COPERNICUSMARINE_SERVICE_USERNAME")
    password: str = Field(default="", validation_alias="COPERNICUSMARINE_SERVICE_PASSWORD")

    @property
    def has_credentials(self) -> bool:
        """Check if Copernicus Marine credentials are provided."""
        return bool(self.username and self.password)


class KaggleSettings(BaseModel):
    """Kaggle API credentials for competition data access."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    username: str = Field(default="", validation_alias="KAGGLE_USERNAME")
    key: str = Field(default="", validation_alias="KAGGLE_KEY")

    @property
    def has_credentials(self) -> bool:
        """Check if Kaggle credentials are provided."""
        return bool(self.username and self.key)


class MLflowSettings(BaseModel):
    """MLflow experiment tracking configuration."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    tracking_uri: str = Field(default="./mlruns", validation_alias="MLFLOW_TRACKING_URI")
    experiment_name: str = Field(
        default="cryolens-benchmarks", validation_alias="MLFLOW_EXPERIMENT_NAME"
    )


class Settings(BaseSettings):
    """Aggregated environment variables and local runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application
    env: str = Field(default="development", validation_alias="CRYOLENS_ENV")
    log_level: str = Field(default="INFO", validation_alias="CRYOLENS_LOG_LEVEL")
    data_dir: Path = Field(default=Path("./data"), validation_alias="CRYOLENS_DATA_DIR")
    cache_dir: Path = Field(default=Path("./data/cache"), validation_alias="CRYOLENS_CACHE_DIR")

    # Sub-configs populated via alias matching
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    postgres_db: str = Field(default="cryolens", validation_alias="POSTGRES_DB")
    postgres_user: str = Field(default="cryolens_user", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(default="", validation_alias="POSTGRES_PASSWORD")

    cdse_username: str = Field(default="", validation_alias="CDSE_USERNAME")
    cdse_password: str = Field(default="", validation_alias="CDSE_PASSWORD")
    cdse_client_id: str = Field(default="", validation_alias="CDSE_CLIENT_ID")
    cdse_client_secret: str = Field(default="", validation_alias="CDSE_CLIENT_SECRET")

    earthdata_username: str = Field(default="", validation_alias="EARTHDATA_USERNAME")
    earthdata_password: str = Field(default="", validation_alias="EARTHDATA_PASSWORD")

    pc_sdk_subscription_key: str = Field(default="", validation_alias="PC_SDK_SUBSCRIPTION_KEY")

    copernicusmarine_service_username: str = Field(
        default="", validation_alias="COPERNICUSMARINE_SERVICE_USERNAME"
    )
    copernicusmarine_service_password: str = Field(
        default="", validation_alias="COPERNICUSMARINE_SERVICE_PASSWORD"
    )

    kaggle_username: str = Field(default="", validation_alias="KAGGLE_USERNAME")
    kaggle_key: str = Field(default="", validation_alias="KAGGLE_KEY")

    mlflow_tracking_uri: str = Field(default="./mlruns", validation_alias="MLFLOW_TRACKING_URI")
    mlflow_experiment_name: str = Field(
        default="cryolens-benchmarks", validation_alias="MLFLOW_EXPERIMENT_NAME"
    )

    @property
    def db(self) -> DatabaseSettings:
        return DatabaseSettings(
            host=self.postgres_host,
            port=self.postgres_port,
            db=self.postgres_db,
            user=self.postgres_user,
            password=self.postgres_password,
        )

    @property
    def cdse(self) -> CDSESettings:
        return CDSESettings(
            username=self.cdse_username,
            password=self.cdse_password,
            client_id=self.cdse_client_id,
            client_secret=self.cdse_client_secret,
        )

    @property
    def earthdata(self) -> EarthdataSettings:
        return EarthdataSettings(
            username=self.earthdata_username,
            password=self.earthdata_password,
        )

    @property
    def planetary_computer(self) -> PlanetaryComputerSettings:
        return PlanetaryComputerSettings(
            subscription_key=self.pc_sdk_subscription_key,
        )

    @property
    def cmems(self) -> CopernicusMarineSettings:
        return CopernicusMarineSettings(
            username=self.copernicusmarine_service_username,
            password=self.copernicusmarine_service_password,
        )

    @property
    def kaggle(self) -> KaggleSettings:
        return KaggleSettings(
            username=self.kaggle_username,
            key=self.kaggle_key,
        )

    @property
    def mlflow(self) -> MLflowSettings:
        return MLflowSettings(
            tracking_uri=self.mlflow_tracking_uri,
            experiment_name=self.mlflow_experiment_name,
        )


# --- YAML Project Config Models ---


class SpatialBBox(BaseModel):
    west: float
    south: float
    east: float
    north: float


class SpatialConfig(BaseModel):
    target_crs: str
    source_crs: str
    pixel_spacing_m: float
    effective_resolution_m: float
    aoi_file: str
    bbox: SpatialBBox


class SeasonConfig(BaseModel):
    peak_months: list[int]
    extended_months: list[int]


class TilingConfig(BaseModel):
    tile_size_px: int
    tile_overlap_px: int
    min_object_dim_px: int


class IIPSizeClass(BaseModel):
    name: str
    length_m: list[float]
    height_m: list[float]
    detectable_ew_grd: bool


class TaxonomyConfig(BaseModel):
    classes: list[str]
    iip_size_classes: list[IIPSizeClass]


class CDSEEndpointConfig(BaseModel):
    stac_url: str
    token_url: str
    odata_url: str
    collection: str
    instrument_mode: str
    polarizations: list[str]
    product_type: str


class MPCEndpointConfig(BaseModel):
    stac_url: str
    collection: str


class CMEMSEndpointConfig(BaseModel):
    live_product_id: str
    hindcast_product_id: str


class EndpointsConfig(BaseModel):
    cdse: CDSEEndpointConfig
    mpc: MPCEndpointConfig
    cmems: CMEMSEndpointConfig


class CFARConfig(BaseModel):
    default_pfa: float
    guard_window: list[int]
    background_window: list[int]
    distribution: str


class ProjectMetadata(BaseModel):
    name: str
    description: str
    version: str


class ProjectConfig(BaseModel):
    """Typed representation of configs/project.yaml."""

    project: ProjectMetadata
    spatial: SpatialConfig
    season: SeasonConfig
    tiling: TilingConfig
    taxonomy: TaxonomyConfig
    endpoints: EndpointsConfig
    cfar: CFARConfig


class AppConfig(BaseModel):
    """Complete unified configuration (credentials + project settings)."""

    settings: Settings
    project: ProjectConfig


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton for environment credentials and settings."""
    return Settings()


@lru_cache(maxsize=1)
def get_project_config(config_path: str = "configs/project.yaml") -> ProjectConfig:
    """Load and parse typed project configuration YAML."""
    path = Path(config_path)
    if not path.is_file():
        # Fallback to search from repository root
        root_path = Path(__file__).resolve().parents[3] / config_path
        if root_path.is_file():
            path = root_path
        else:
            raise FileNotFoundError(f"Project configuration file not found at: {config_path}")

    with open(path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)

    return ProjectConfig(**data)


def get_app_config() -> AppConfig:
    """Get unified application configuration."""
    return AppConfig(
        settings=get_settings(),
        project=get_project_config(),
    )
