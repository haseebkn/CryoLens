"""Alternative Sentinel-2 provider: original CDSE SAFE products with explicit verification."""

from __future__ import annotations

import json
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import rasterio
import requests
from shapely.geometry import shape

from cryolens.config.settings import get_settings
from cryolens.eval.cohort import file_digest
from cryolens.geo.aoi import load_aoi
from cryolens.ingest.cdse import CDSEClient
from cryolens.ingest.sentinel2 import BANDS, copy_native_window, utc
from cryolens.ingest.sentinel2_safe import product_identity, same_acquisition, verify_safe_archive

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD = "https://download.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_HOSTS = {"download.dataspace.copernicus.eu", "zipper.dataspace.copernicus.eu"}


class CDSESentinel2Client:
    provider = "cdse"

    def __init__(
        self,
        session: requests.Session | None = None,
        cache: Path | None = None,
        token: Callable[[], str] | None = None,
        max_bytes: int = 2 * 1024**3,
    ) -> None:
        self.session = session or requests.Session()
        self.cache = cache if cache is not None else get_settings().data_dir / "raw/sentinel2-cdse"
        self.token = token or CDSEClient().get_auth_token
        self.max_bytes = max_bytes

    def _query(self, expression: str) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "$filter": expression,
            "$expand": "Attributes",
            "$orderby": "ContentDate/Start asc,Name asc",
            "$top": 100,
        }
        url = CATALOGUE
        query: dict[str, str | int] | None = params
        records = []
        for _ in range(50):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "catalogue.dataspace.copernicus.eu"
                or parsed.path != "/odata/v1/Products"
            ):
                raise ValueError("Untrusted CDSE catalogue pagination")
            response = self.session.get(url, params=query, timeout=60)
            response.raise_for_status()
            page = response.json()
            records.extend(page["value"])
            next_link = page.get("@odata.nextLink")
            if not next_link:
                break
            url, query = urljoin(url, next_link), None
        else:
            raise RuntimeError("CDSE catalogue pagination incomplete")
        return {
            "endpoint": CATALOGUE,
            "query": params,
            "queried_at": datetime.now(UTC).isoformat(),
            "complete": True,
            "products": records,
        }

    @staticmethod
    def item(product: dict[str, Any]) -> dict[str, Any]:
        identity = product_identity(product["Name"])
        return {
            "id": product["Name"].removesuffix(".SAFE"),
            "provider": "cdse",
            "geometry": product["GeoFootprint"],
            "properties": {
                "datetime": product["ContentDate"]["Start"],
                "platform": identity["platform"],
                "s2:product_uri": product["Name"],
                "s2:processing_baseline": identity["baseline"][:2] + "." + identity["baseline"][2:],
            },
            "assets": {},
            "cdse_product": product,
        }

    def search(
        self, footprint: dict[str, Any], acquired: datetime, hours: float = 12
    ) -> dict[str, Any]:
        region = shape(footprint).intersection(load_aoi())
        if region.is_empty:
            raise ValueError("CDSE optical search must overlap the NL study polygon")
        acquired = utc(acquired)
        start, end = acquired - timedelta(hours=hours), acquired + timedelta(hours=hours)
        expression = (
            "Collection/Name eq 'SENTINEL-2' and contains(Name,'_MSIL2A_') "
            f"and ContentDate/Start ge {start.isoformat()} and ContentDate/Start le {end.isoformat()} "
            f"and OData.CSC.Intersects(area=geography'SRID=4326;{region.wkt}')"
        )
        receipt = self._query(expression)
        items = {}
        for product in receipt.pop("products"):
            when = utc(product["ContentDate"]["Start"])
            if (
                not shape(product["GeoFootprint"]).intersects(region)
                or abs((when - acquired).total_seconds()) > hours * 3600
            ):
                continue
            item = self.item(product)
            items[item["id"]] = item
        receipt.update(
            provider=self.provider,
            items=sorted(
                items.values(),
                key=lambda i: (
                    abs((utc(i["properties"]["datetime"]) - acquired).total_seconds()),
                    -int(product_identity(i["cdse_product"]["Name"])["baseline"]),
                    i["id"],
                ),
            ),
        )
        return receipt

    def match_product(self, reference: dict[str, Any]) -> dict[str, Any]:
        name = reference["properties"]["s2:product_uri"]
        identity = product_identity(name)
        exact = self._query("Collection/Name eq 'SENTINEL-2' and Name eq '" + name + "'")
        products = [p for p in exact["products"] if p["Name"] == name]
        searches = [exact]
        if not any(p.get("Online") is True for p in products):
            broader = self._query(
                f"Collection/Name eq 'SENTINEL-2' and startswith(Name,'{identity['platform']}_MSIL2A_{identity['sensing']}_') and contains(Name,'_R{identity['orbit']}_T{identity['tile']}_')"
            )
            searches.append(broader)
            products = [p for p in broader["products"] if same_acquisition(name, p["Name"])]
        if not products:
            raise ValueError("No CDSE product for the same platform/acquisition/orbit/tile")
        online = [p for p in products if p.get("Online") is True]
        if not online:
            raise ValueError("Matched CDSE products are offline")
        selected = sorted(
            online,
            key=lambda p: (int(product_identity(p["Name"])["baseline"]), p["Name"]),
            reverse=True,
        )[0]
        return {
            "reference_product_name": name,
            "match_kind": "exact_product"
            if selected["Name"] == name
            else "same_acquisition_reprocessed",
            "selected": self.item(selected),
            "searches": searches,
        }

    def _stream(self, url: str) -> requests.Response:
        token = self.token()
        for _ in range(6):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in DOWNLOAD_HOSTS
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Refusing untrusted CDSE download redirect")
            response = self.session.get(
                url,
                headers={"Authorization": "Bearer " + token},
                stream=True,
                allow_redirects=False,
                timeout=(30, 120),
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ValueError("CDSE redirect lacks a location")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                raise RuntimeError(
                    f"CDSE download HTTP {status}; credentials or product access may need attention"
                )
            return response
        raise RuntimeError("Too many CDSE download redirects")

    def download_product(self, item: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        if item.get("provider") != self.provider:
            raise ValueError("CDSE provider cannot silently substitute another provider's item")
        product = item["cdse_product"]
        identifier = str(uuid.UUID(product["Id"]))
        product_identity(product["Name"])
        if product.get("Online") is not True:
            raise ValueError("CDSE product is offline")
        if int(product.get("ContentLength", 0)) > self.max_bytes:
            raise ValueError("CDSE product exceeds download budget")
        if not shape(product["GeoFootprint"]).intersects(load_aoi()):
            raise ValueError("CDSE product does not overlap the NL study polygon")
        folder = (self.cache.resolve() / identifier).resolve()
        if not folder.is_relative_to(self.cache.resolve()):
            raise ValueError("CDSE cache directory escapes the configured cache")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "product.zip"
        if not path.is_file():
            temporary = folder / "product.zip.part"
            downloaded = 0
            with (
                self._stream(f"{DOWNLOAD}({identifier})/$value") as response,
                temporary.open("wb") as target,
            ):
                for chunk in response.iter_content(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > self.max_bytes:
                        raise ValueError("CDSE download exceeds byte budget")
                    target.write(chunk)
            verification = verify_safe_archive(temporary, product, self.max_bytes)
            temporary.replace(path)
        else:
            verification = verify_safe_archive(path, product, self.max_bytes)
        verification["download_url"] = f"{DOWNLOAD}({identifier})/$value"
        (folder / "product.json").write_text(json.dumps(product, indent=2), encoding="utf-8")
        (folder / "verification.json").write_text(
            json.dumps(verification, indent=2), encoding="utf-8"
        )
        return path, verification

    def download_windows(
        self, item: dict[str, Any], bounds_3978: tuple[float, float, float, float], output: Path
    ) -> dict[str, Any]:
        path, verification = self.download_product(item)
        output.mkdir(parents=True, exist_ok=True)
        receipt = {}
        with zipfile.ZipFile(path) as archive:
            for band in BANDS:
                member = verification["bands"][band]
                local = path.parent / f"{band}-source.jp2"
                expected = verification["manifest_files"][member]
                # Always refresh our managed band cache from the verified archive.
                temporary = local.with_suffix(".jp2.part")
                with archive.open(member) as source_file, temporary.open("wb") as target:
                    while chunk := source_file.read(1024 * 1024):
                        target.write(chunk)
                temporary.replace(local)
                with rasterio.open(local) as source:
                    tile = product_identity(verification["product_name"])["tile"]
                    expected_epsg = (32600 if tile[2] >= "N" else 32700) + int(tile[:2])
                    if source.crs is None or source.crs.to_epsg() != expected_epsg:
                        raise ValueError("SAFE band CRS differs from its declared MGRS tile")
                    asset = copy_native_window(
                        source,
                        band,
                        bounds_3978,
                        output,
                        verification["download_url"],
                        verification["special_values"]["NODATA"],
                    )
                asset.update(
                    provider=self.provider,
                    source_member=member,
                    publisher_file_checksum=expected,
                    source_file_sha256=file_digest(local),
                    archive_sha256=verification["archive_sha256"],
                    processing_baseline=verification["processing_baseline"],
                )
                if band != "SCL":
                    asset["reflectance_encoding"] = {
                        "quantification_value": verification["boa_quantification_value"],
                        "boa_add_offset_dn": verification["boa_add_offsets_dn"][band],
                    }
                receipt[band] = asset
        (output / "cdse-verification.json").write_text(
            json.dumps(verification, indent=2), encoding="utf-8"
        )
        (output / "item.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
        return receipt
