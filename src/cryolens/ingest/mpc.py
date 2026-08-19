"""Microsoft Planetary Computer client for signed windowed SAR reads."""

import logging
from datetime import datetime
from typing import Any

from cryolens.config.settings import PlanetaryComputerSettings, get_app_config
from cryolens.ingest.cdse import SARSceneMetadata

logger = logging.getLogger(__name__)


class MPCClient:
    """Client for Microsoft Planetary Computer Sentinel-1 STAC API."""

    def __init__(self, mpc_settings: PlanetaryComputerSettings | None = None) -> None:
        app_config = get_app_config()
        self.settings = mpc_settings or app_config.settings.planetary_computer
        self.endpoints = app_config.project.endpoints

    def search_scenes(
        self,
        bbox: list[float] | None = None,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        instrument_mode: str = "EW",
        limit: int = 50,
        sign_assets: bool = True,
    ) -> list[tuple[SARSceneMetadata, Any]]:
        """Search Sentinel-1 GRD scenes on Planetary Computer and return signed STAC items."""
        try:
            import planetary_computer as pc
            import pystac_client
        except ImportError as exc:
            raise ImportError(
                "planetary-computer and pystac-client are required. "
                "Install via `pip install planetary-computer pystac-client`."
            ) from exc

        stac_url = self.endpoints.mpc.stac_url
        collection = self.endpoints.mpc.collection  # "sentinel-1-grd"

        datetime_query = None
        if start_date and end_date:
            s_str = start_date.isoformat() if isinstance(start_date, datetime) else start_date
            e_str = end_date.isoformat() if isinstance(end_date, datetime) else end_date
            datetime_query = f"{s_str}/{e_str}"
        elif start_date:
            s_str = start_date.isoformat() if isinstance(start_date, datetime) else start_date
            datetime_query = f"{s_str}/.."

        query_params: dict[str, Any] = {
            "sar:instrument_mode": {"eq": instrument_mode},
        }

        logger.info("Connecting to Planetary Computer STAC at %s...", stac_url)
        catalog = pystac_client.Client.open(stac_url)
        search = catalog.search(
            collections=[collection],
            bbox=bbox,
            datetime=datetime_query,
            query=query_params,
            limit=limit,
        )

        results: list[tuple[SARSceneMetadata, Any]] = []
        for item in search.items():
            if sign_assets:
                signed_item = pc.sign(item)
            else:
                signed_item = item

            metadata = self._parse_mpc_item(signed_item)
            if metadata:
                results.append((metadata, signed_item))

        logger.info("Found %d Planetary Computer scenes.", len(results))
        return results

    def _parse_mpc_item(self, item: Any) -> SARSceneMetadata | None:
        """Parse PySTAC Item into SARSceneMetadata."""
        props = item.properties
        scene_id = item.id
        if not scene_id:
            return None

        dt = item.datetime or datetime(2020, 1, 1)
        start_time = item.properties.get("start_datetime")
        end_time = item.properties.get("end_datetime")

        start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00")) if start_time else dt
        end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00")) if end_time else dt

        pols = props.get("sar:polarizations") or ["HH", "HV"]
        if isinstance(pols, str):
            pols = [pols]

        # Asset URL for HH or HV raster
        download_url = None
        if "vh" in item.assets:
            download_url = item.assets["vh"].href
        elif "hv" in item.assets:
            download_url = item.assets["hv"].href
        elif "hh" in item.assets:
            download_url = item.assets["hh"].href

        return SARSceneMetadata(
            scene_id=scene_id,
            platform=props.get("platform") or "Sentinel-1",
            instrument_mode=props.get("sar:instrument_mode") or "EW",
            polarizations=pols,
            product_type=props.get("sar:product_type") or "GRD",
            acquisition_time=dt,
            start_time=start_dt,
            end_time=end_dt,
            footprint_geojson=item.geometry,
            orbit_direction=props.get("sat:orbit_state"),
            absolute_orbit=props.get("sat:absolute_orbit"),
            relative_orbit=props.get("sat:relative_orbit"),
            download_url=download_url,
            stac_item_id=item.id,
            properties=props,
        )
