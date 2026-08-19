"""ASF DAAC client for bulk historical Sentinel-1 catalog queries."""

import logging
from datetime import datetime
from typing import Any

from cryolens.config.settings import EarthdataSettings, get_app_config
from cryolens.ingest.cdse import SARSceneMetadata

logger = logging.getLogger(__name__)


class ASFClient:
    """Query client for Alaska Satellite Facility (ASF) DAAC."""

    def __init__(self, earthdata_settings: EarthdataSettings | None = None) -> None:
        app_config = get_app_config()
        self.settings = earthdata_settings or app_config.settings.earthdata
        self._session: Any = None

    def search_scenes(
        self,
        bbox: list[float] | None = None,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
        beam_mode: str = "EW",
        polarization: list[str] | None = None,
        max_results: int = 50,
    ) -> list[SARSceneMetadata]:
        """Search Sentinel-1 historical scenes via ASF Search API."""
        try:
            import asf_search as asf
        except ImportError as exc:
            raise ImportError(
                "asf-search is required. Install via `pip install asf-search`."
            ) from exc

        pols = polarization or ["HH+HV", "HH", "HV"]
        wkt_polygon = None
        if bbox:
            w, s, e, n = bbox
            wkt_polygon = f"POLYGON(({w} {s}, {e} {s}, {e} {n}, {w} {n}, {w} {s}))"

        start_str = start_date.isoformat() if isinstance(start_date, datetime) else start_date
        end_str = end_date.isoformat() if isinstance(end_date, datetime) else end_date

        logger.info("Executing ASF DAAC search (beam_mode=%s, pols=%s)...", beam_mode, pols)

        search_opts = {
            "platform": asf.PLATFORM.SENTINEL1,
            "processingLevel": asf.PRODUCT_TYPE.GRD,
            "beamMode": beam_mode,
            "polarization": pols,
            "maxResults": max_results,
        }
        if wkt_polygon:
            search_opts["intersectsWith"] = wkt_polygon
        if start_str:
            search_opts["start"] = start_str
        if end_str:
            search_opts["end"] = end_str

        # Add authenticated session if credentials exist
        auth_session = None
        if self.settings.has_credentials:
            try:
                auth_session = asf.ASFSession().auth_with_creds(
                    self.settings.username, self.settings.password
                )
            except Exception as exc:
                logger.warning("ASF authentication failed, proceeding unauthenticated: %s", exc)

        results = asf.search(**search_opts, session=auth_session)
        logger.info("ASF query returned %d scenes.", len(results))

        parsed_list: list[SARSceneMetadata] = []
        for product in results:
            parsed = self._convert_asf_product(product)
            if parsed:
                parsed_list.append(parsed)

        return parsed_list

    def _convert_asf_product(self, product: Any) -> SARSceneMetadata | None:
        """Convert ASFProduct dictionary or object to SARSceneMetadata."""
        props = product.properties if hasattr(product, "properties") else product
        scene_id = props.get("sceneName") or props.get("fileID") or ""
        if not scene_id:
            return None

        start_time_str = props.get("startTime") or "2020-01-01T00:00:00Z"
        stop_time_str = props.get("stopTime") or start_time_str
        clean_start = start_time_str.replace("Z", "+00:00")
        clean_stop = stop_time_str.replace("Z", "+00:00")

        try:
            start_dt = datetime.fromisoformat(clean_start)
            stop_dt = datetime.fromisoformat(clean_stop)
        except ValueError:
            start_dt = stop_dt = datetime(2020, 1, 1)

        pol_raw = props.get("polarization") or "HH+HV"
        pols = pol_raw.split("+") if "+" in pol_raw else [pol_raw]

        geojson_geom = product.geometry if hasattr(product, "geometry") else {}

        return SARSceneMetadata(
            scene_id=scene_id,
            platform=props.get("platform") or "Sentinel-1",
            instrument_mode=props.get("beamModeType") or "EW",
            polarizations=pols,
            product_type=props.get("processingLevel") or "GRD",
            acquisition_time=start_dt,
            start_time=start_dt,
            end_time=stop_dt,
            footprint_geojson=geojson_geom,
            orbit_direction=props.get("flightDirection"),
            absolute_orbit=props.get("orbit"),
            relative_orbit=props.get("pathNumber"),
            download_url=props.get("url"),
            properties=props,
        )
