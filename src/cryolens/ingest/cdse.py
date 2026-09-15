"""Copernicus Data Space Ecosystem (CDSE) STAC and OData catalog client."""

import logging
import re
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from cryolens.config.settings import CDSESettings, EndpointsConfig, get_app_config
from cryolens.geo.aoi import load_aoi, scene_intersects_aoi
from cryolens.ingest.cache import LocalCacheManager

logger = logging.getLogger(__name__)


class SARSceneMetadata(BaseModel):
    """Normalized metadata record for a Sentinel-1 SAR acquisition."""

    scene_id: str
    platform: str
    instrument_mode: str
    polarizations: list[str]
    product_type: str
    acquisition_time: datetime
    start_time: datetime
    end_time: datetime
    footprint_geojson: dict[str, Any]
    orbit_direction: str | None = None
    absolute_orbit: int | None = None
    relative_orbit: int | None = None
    download_url: str | None = None
    stac_item_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class CDSEClient:
    """Client for Copernicus Data Space Ecosystem STAC & OData services."""

    def __init__(
        self,
        cdse_settings: CDSESettings | None = None,
        endpoints_config: EndpointsConfig | None = None,
        cache_manager: LocalCacheManager | None = None,
    ) -> None:
        app_config = get_app_config()
        self.settings = cdse_settings or app_config.settings.cdse
        self.endpoints = endpoints_config or app_config.project.endpoints
        self.cache = cache_manager or LocalCacheManager(cache_dir=app_config.settings.cache_dir)

        self._token: str | None = None
        self._token_expiry: float = 0.0

    def get_auth_token(self) -> str:
        """Obtain or refresh Keycloak OAuth2 bearer access token."""
        now = time.time()
        if self._token and now < (self._token_expiry - 60):
            return self._token

        if not self.settings.has_credentials:
            raise ValueError(
                "CDSE credentials missing. Please set CDSE_USERNAME/CDSE_PASSWORD or "
                "CDSE_CLIENT_ID/CDSE_CLIENT_SECRET in .env."
            )

        token_url = self.endpoints.cdse.token_url
        data: dict[str, str] = {}

        if self.settings.client_id and self.settings.client_secret:
            data = {
                "grant_type": "client_credentials",
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
            }
        else:
            data = {
                "grant_type": "password",
                "client_id": "cdse-public",
                "username": self.settings.username,
                "password": self.settings.password,
            }

        logger.debug("Requesting OAuth2 token from CDSE auth endpoint...")
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(token_url, data=data)
            resp.raise_for_status()
            payload = resp.json()
            self._token = payload["access_token"]
            expires_in = payload.get("expires_in", 600)
            self._token_expiry = now + float(expires_in)

        return self._token

    def search_scenes(
        self,
        bbox: list[float] | None = None,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        instrument_mode: str | None = None,
        polarizations: list[str] | None = None,
        product_type: str | None = None,
        limit: int = 50,
    ) -> list[SARSceneMetadata]:
        """Search Sentinel-1 GRD scenes matching spatial, temporal, and mode criteria."""
        collection_id = self.endpoints.cdse.collection  # "sentinel-1-grd"
        stac_url = f"{self.endpoints.cdse.stac_url}/search"

        mode = instrument_mode or self.endpoints.cdse.instrument_mode
        pols = polarizations or self.endpoints.cdse.polarizations
        prod = product_type or self.endpoints.cdse.product_type

        # Format datetime range string
        datetime_query: str | None = None
        if start_date and end_date:
            s_str = start_date.isoformat() if isinstance(start_date, datetime) else start_date
            e_str = end_date.isoformat() if isinstance(end_date, datetime) else end_date
            datetime_query = f"{s_str}/{e_str}"
        elif start_date:
            s_str = start_date.isoformat() if isinstance(start_date, datetime) else start_date
            datetime_query = f"{s_str}/.."
        elif end_date:
            e_str = end_date.isoformat() if isinstance(end_date, datetime) else end_date
            datetime_query = f"../{e_str}"

        query_payload: dict[str, Any] = {
            "collections": [collection_id],
            "limit": limit,
        }
        query_payload["bbox"] = bbox or list(load_aoi().bounds)
        if datetime_query:
            query_payload["datetime"] = datetime_query

        # Property filters for SAR EW GRD HH+HV
        properties_query: dict[str, Any] = {}
        if mode:
            properties_query["sar:instrument_mode"] = {"eq": mode}
        if prod:
            properties_query["sar:product_type"] = {"eq": prod}
        if pols:
            properties_query["sar:polarizations"] = {"contains": pols[0]}

        if properties_query:
            query_payload["query"] = properties_query

        headers = {"Content-Type": "application/json"}
        if self.settings.has_credentials:
            try:
                headers["Authorization"] = f"Bearer {self.get_auth_token()}"
            except Exception as e:
                logger.warning("Proceeding with unauthenticated STAC search: %s", e)

        logger.info("Executing CDSE STAC search against %s...", stac_url)
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(stac_url, json=query_payload, headers=headers)
            resp.raise_for_status()
            feature_collection = resp.json()

        results: list[SARSceneMetadata] = []
        for feature in feature_collection.get("features", []):
            parsed = self._parse_stac_feature(feature)
            if (
                parsed
                and {p.upper() for p in pols}.issubset(set(parsed.polarizations))
                and scene_intersects_aoi(parsed.footprint_geojson)
            ):
                results.append(parsed)

        logger.info("Found %d Sentinel-1 scenes matching criteria.", len(results))
        return results

    def download_scene(
        self,
        scene: SARSceneMetadata | str,
        output_dir: Path | str = "./data/raw",
        extract: bool = True,
    ) -> Path:
        """Download a SAFE archive atomically, checking ZIP CRCs and extraction paths."""
        scene_id = scene.scene_id if isinstance(scene, SARSceneMetadata) else scene
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.SAFE)?", scene_id):
            raise ValueError("Scene identifier must be a safe product basename.")
        scene_id = scene_id.removesuffix(".SAFE")
        target_root = Path(output_dir).resolve() / scene_id
        target_root.mkdir(parents=True, exist_ok=True)

        expected_safe_dir = target_root / f"{scene_id}.SAFE"
        if (expected_safe_dir / "manifest.safe").is_file() and (
            expected_safe_dir / "measurement"
        ).is_dir():
            logger.info("Scene %s already present in %s", scene_id, expected_safe_dir)
            return expected_safe_dir

        token = self.get_auth_token()
        headers = {"Authorization": f"Bearer {token}"}

        # Resolve download URL via OData if not in metadata
        download_url = scene.download_url if isinstance(scene, SARSceneMetadata) else None
        if not download_url:
            download_url = f"{self.endpoints.cdse.odata_url}/Products?$filter=Name eq '{scene_id}'"
            with httpx.Client(timeout=30.0) as client:
                res = client.get(download_url, headers=headers)
                res.raise_for_status()
                data = res.json()
                items = data.get("value", [])
                if not items:
                    raise FileNotFoundError(f"Product {scene_id} not found in CDSE catalog.")
                product_id = items[0]["Id"]
                download_url = f"{self.endpoints.cdse.odata_url}/Products({product_id})/$value"

        zip_path = target_root / f"{scene_id}.zip"
        parsed_url = urlsplit(download_url)
        host = parsed_url.hostname or ""
        if parsed_url.scheme != "https" or not (
            host == "dataspace.copernicus.eu" or host.endswith(".dataspace.copernicus.eu")
        ):
            raise ValueError("Refusing to send CDSE credentials to a non-CDSE HTTPS download URL.")
        partial_path = zip_path.with_suffix(".zip.part")
        logger.info("Downloading %s from CDSE...", scene_id)

        with httpx.Client(timeout=600.0, follow_redirects=True) as client:
            with client.stream("GET", download_url, headers=headers) as response:
                response.raise_for_status()
                bytes_downloaded = 0
                with open(partial_path, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        bytes_downloaded += len(chunk)

                self.cache.record_transfer(bytes_downloaded)
        with zipfile.ZipFile(partial_path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Downloaded archive failed ZIP CRC validation.")
        partial_path.replace(zip_path)

        if extract:
            logger.info("Extracting %s...", zip_path.name)
            with tempfile.TemporaryDirectory(prefix="extract-", dir=target_root) as temp_dir:
                extract_root = Path(temp_dir).resolve()
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    for member in zip_ref.infolist():
                        destination = (extract_root / member.filename).resolve()
                        if not destination.is_relative_to(extract_root) or "\\" in member.filename:
                            raise ValueError("Unsafe path in downloaded SAFE archive.")
                        if (member.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError("Symbolic links are not allowed in SAFE archives.")
                    zip_ref.extractall(extract_root)
                extracted = extract_root / expected_safe_dir.name
                if (
                    not (extracted / "manifest.safe").is_file()
                    or not (extracted / "measurement").is_dir()
                ):
                    raise ValueError(
                        "Downloaded archive does not contain the expected SAFE product."
                    )
                if expected_safe_dir.exists():
                    raise ValueError(
                        "Incomplete SAFE directory already exists; remove it before retrying."
                    )
                extracted.replace(expected_safe_dir)
            zip_path.unlink()  # Remove zip after extraction to save disk space

        self.cache.evict_if_needed()
        return expected_safe_dir if extract else zip_path

    def _parse_stac_feature(self, feature: dict[str, Any]) -> SARSceneMetadata | None:
        """Parse raw STAC Feature JSON into typed SARSceneMetadata."""
        props = feature.get("properties", {})
        scene_id = feature.get("id", "")
        if not scene_id:
            return None

        # Parse datetime
        dt_str = props.get("datetime") or props.get("start_datetime")
        if not dt_str:
            logger.warning("Skipping STAC scene without acquisition time: %s", scene_id)
            return None
        start_str = props.get("start_datetime") or dt_str
        end_str = props.get("end_datetime") or dt_str

        try:
            acq_time = self._parse_iso(dt_str)
            start_time = self._parse_iso(start_str)
            end_time = self._parse_iso(end_str)
        except ValueError:
            logger.warning("Skipping STAC scene with invalid time: %s", scene_id)
            return None

        # Polarizations list
        pols = props.get("sar:polarizations") or []
        if isinstance(pols, str):
            pols = [pols]

        download_url = None
        assets = feature.get("assets", {})
        if "PRODUCT" in assets:
            download_url = assets["PRODUCT"].get("href")
        elif "download" in assets:
            download_url = assets["download"].get("href")

        return SARSceneMetadata(
            scene_id=scene_id,
            platform=props.get("platform") or "Sentinel-1",
            instrument_mode=props.get("sar:instrument_mode") or "EW",
            polarizations=pols,
            product_type=props.get("sar:product_type") or "GRD",
            acquisition_time=acq_time,
            start_time=start_time,
            end_time=end_time,
            footprint_geojson=feature.get("geometry", {}),
            orbit_direction=props.get("sat:orbit_state"),
            absolute_orbit=props.get("sat:absolute_orbit"),
            relative_orbit=props.get("sat:relative_orbit"),
            download_url=download_url,
            stac_item_id=feature.get("id"),
            properties=props,
        )

    def _parse_iso(self, dt_str: str) -> datetime:
        """Safely parse ISO datetime string."""
        clean_str = dt_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(clean_str)
        if parsed.tzinfo is None:
            raise ValueError("Catalogue acquisition time must include a timezone.")
        return parsed.astimezone(UTC)
