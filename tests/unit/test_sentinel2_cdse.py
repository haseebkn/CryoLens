"""Real-format synthetic fixtures for CDSE integrity, redirects, matching and baseline encoding."""

import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock
from xml.sax.saxutils import escape

import numpy as np
import pytest
import rasterio
import requests
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from cryolens.eval.cohort import file_digest
from cryolens.ingest.sentinel2 import BANDS
from cryolens.ingest.sentinel2_cdse import CATALOGUE, DOWNLOAD, CDSESentinel2Client
from cryolens.ingest.sentinel2_safe import (
    read_product_metadata,
    same_acquisition,
    verify_safe_archive,
)
from cryolens.review.verify_optical import compare_native_band

NAME = "S2B_MSIL2A_20200515T120000_N0500_R082_T22UFT_20230705T144556.SAFE"
OLD = NAME.replace("N0500", "N0212").replace("20230705T144556", "20201013T035446")
UUID = "b84aee26-fde4-47f9-a376-33d52ec2a14c"


def metadata(name: str = NAME, offsets: bool = True) -> bytes:
    baseline = "05.00" if "N0500" in name else "02.12"
    additions = (
        "".join(f'<BOA_ADD_OFFSET band_id="{n}">-1000</BOA_ADD_OFFSET>' for n in (1, 2, 3, 7))
        if offsets
        else ""
    )
    return f"""<root><PRODUCT_URI>{name}</PRODUCT_URI><PROCESSING_LEVEL>Level-2A</PROCESSING_LEVEL><PROCESSING_BASELINE>{baseline}</PROCESSING_BASELINE><PRODUCT_START_TIME>2020-05-15T12:00:00Z</PRODUCT_START_TIME><BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>{additions}<Special_Values><SPECIAL_VALUE_TEXT>NODATA</SPECIAL_VALUE_TEXT><SPECIAL_VALUE_INDEX>0</SPECIAL_VALUE_INDEX></Special_Values><Special_Values><SPECIAL_VALUE_TEXT>SATURATED</SPECIAL_VALUE_TEXT><SPECIAL_VALUE_INDEX>65535</SPECIAL_VALUE_INDEX></Special_Values></root>""".encode()


def safe_fixture(tmp_path: Path, defect: str = "") -> tuple[Path, dict[str, Any]]:
    files = {"MTD_MSIL2A.xml": metadata()}
    if defect == "wrong_metadata":
        files["MTD_MSIL2A.xml"] = metadata().replace(b"Level-2A", b"Level-1C")
    if defect == "missing_offsets":
        files["MTD_MSIL2A.xml"] = metadata(offsets=False)
    x, y = Transformer.from_crs(4326, 32622, always_xy=True).transform(-52.5, 47.5)
    for band in BANDS:
        spacing = 20 if band == "SCL" else 10
        source = tmp_path / f"{band}.tif"
        size = 200 // spacing
        values = np.full((size, size), 6 if band == "SCL" else 1500, dtype=np.uint16)
        values[0, 0] = 0
        with rasterio.open(
            source,
            "w",
            driver="GTiff",
            width=size,
            height=size,
            count=1,
            dtype="uint16",
            crs="EPSG:32622",
            transform=from_origin(x - 100, y + 100, spacing, spacing),
            nodata=0,
        ) as target:
            target.write(values, 1)
        # Unit fixture pixels are TIFF-encoded; the live pilot separately exercises actual JP2.
        files[
            f"GRANULE/L2A_T22UFT_TEST/IMG_DATA/R{spacing}m/T22UFT_20200515T120000_{band}_{spacing}m.jp2"
        ] = source.read_bytes()
    manifest_entries = []
    for location, body in files.items():
        checksum = hashlib.sha3_256(body).hexdigest()
        if defect == "bad_manifest" and location == "MTD_MSIL2A.xml":
            checksum = "0" * 64
        manifest_entries.append(
            f'<byteStream size="{len(body)}"><fileLocation href="./{escape(location)}"/><checksum checksumName="SHA3-256">{checksum}</checksum></byteStream>'
        )
    files["manifest.safe"] = ("<root>" + "".join(manifest_entries) + "</root>").encode()
    if defect == "traversal":
        files["../escape.txt"] = b"unsafe"
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for location, body in files.items():
            archive.writestr(NAME + "/" + location, body)
    product = {
        "Id": UUID,
        "Name": NAME,
        "Online": True,
        "ContentLength": path.stat().st_size,
        "ContentDate": {"Start": "2020-05-15T12:00:00Z"},
        "GeoFootprint": mapping(box(-53, 47, -52, 48)),
        "Checksum": [
            {
                "Algorithm": "MD5",
                "Value": hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest(),
            }
        ],
    }
    return path, product


def test_verified_safe_checks_catalogue_manifest_and_declared_metadata(tmp_path: Path) -> None:
    path, product = safe_fixture(tmp_path)
    result = verify_safe_archive(path, product)
    assert result["zip_crc_verified"] is True
    assert result["manifest_checksums_verified"] == 6
    assert result["catalogue_checksums_verified"][0]["algorithm"] == "MD5"
    assert result["boa_add_offsets_dn"]["B04"] == -1000
    assert result["boa_quantification_value"] == 10000
    assert set(result["bands"]) == set(BANDS)


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("bad_manifest", "publisher manifest"),
        ("traversal", "Unsafe"),
        ("wrong_metadata", "identity/processing"),
        ("missing_offsets", "declared BOA"),
    ],
)
def test_safe_integrity_defects_are_rejected(tmp_path: Path, defect: str, reason: str) -> None:
    path, product = safe_fixture(tmp_path, defect)
    with pytest.raises(ValueError, match=reason):
        verify_safe_archive(path, product)


def test_archive_checksum_length_and_budget_are_enforced(tmp_path: Path) -> None:
    path, product = safe_fixture(tmp_path)
    with pytest.raises(ValueError, match="catalogue MD5"):
        verify_safe_archive(
            path, {**product, "Checksum": [{"Algorithm": "MD5", "Value": "0" * 32}]}
        )
    with pytest.raises(ValueError, match="length differs"):
        verify_safe_archive(path, {**product, "ContentLength": path.stat().st_size + 1})
    with pytest.raises(ValueError, match="budget"):
        verify_safe_archive(path, product, max_bytes=1)


def test_same_acquisition_requires_platform_time_orbit_and_tile() -> None:
    assert same_acquisition(NAME, OLD)
    for old, new in (
        ("S2B", "S2A"),
        ("T120000", "T120001"),
        ("R082", "R081"),
        ("T22UFT", "T22UFU"),
    ):
        assert not same_acquisition(NAME, NAME.replace(old, new))
    with pytest.raises(ValueError):
        same_acquisition(NAME, "../../unsafe")


def test_older_and_modern_metadata_offsets_are_not_interchangeable() -> None:
    assert read_product_metadata(metadata(OLD, False), OLD)["boa_add_offsets_dn"]["B04"] == 0
    with pytest.raises(ValueError, match="declared BOA"):
        read_product_metadata(metadata(NAME, False), NAME)
    with pytest.raises(ValueError, match="Invalid BOA"):
        read_product_metadata(metadata().replace(b"10000", b"NaN"), NAME)
    with pytest.raises(ValueError, match="Unsupported"):
        read_product_metadata(b'<!DOCTYPE root [<!ENTITY bad "value">]>' + metadata(), NAME)


def test_product_matching_prefers_exact_and_labels_reprocessing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, product = safe_fixture(tmp_path)
    client = CDSESentinel2Client(cache=tmp_path, token=lambda: "test-only-key")
    reference = {"properties": {"s2:product_uri": OLD}}
    searches = Mock(side_effect=[{"products": []}, {"products": [product]}])
    monkeypatch.setattr(client, "_query", searches)
    match = client.match_product(reference)
    assert match["match_kind"] == "same_acquisition_reprocessed"
    assert searches.call_count == 2
    monkeypatch.setattr(
        client, "_query", lambda expression: {"products": [{**product, "Name": OLD}]}
    )
    assert client.match_product(reference)["match_kind"] == "exact_product"


def test_redirects_never_send_bearer_to_untrusted_hosts() -> None:
    session = Mock(spec=requests.Session)
    redirect = Mock(status_code=302, headers={"Location": "https://example.com/stolen"})
    session.get.return_value = redirect
    client = CDSESentinel2Client(
        session=cast(requests.Session, session), token=lambda: "test-only-key"
    )
    with pytest.raises(ValueError, match="untrusted"):
        client._stream(f"{DOWNLOAD}({UUID})/$value")
    assert session.get.call_count == 1
    assert session.get.call_args.kwargs["allow_redirects"] is False
    redirect.close.assert_called_once()


def test_catalogue_pagination_and_nl_time_filters(tmp_path: Path) -> None:
    _, product = safe_fixture(tmp_path)
    session = Mock(spec=requests.Session)
    response = Mock()
    wrong = {**product, "Id": "outside", "GeoFootprint": mapping(box(0, 0, 1, 1))}
    later = {**product, "Id": "late", "ContentDate": {"Start": "2020-05-20T12:00:00Z"}}
    response.json.side_effect = [
        {"value": [product, wrong], "@odata.nextLink": CATALOGUE + "?$skip=100"},
        {"value": [later]},
    ]
    session.get.return_value = response
    client = CDSESentinel2Client(
        session=cast(requests.Session, session), token=lambda: "test-only-key"
    )
    result = client.search(mapping(box(-53, 47, -52, 48)), datetime(2020, 5, 15, 12, tzinfo=UTC))
    assert len(result["items"]) == 1 and result["complete"] is True
    assert session.get.call_count == 2
    assert "cloud" not in result["query"]["$filter"].lower()
    response.json.side_effect = None
    response.json.return_value = {"value": [], "@odata.nextLink": "https://example.com/pagination"}
    with pytest.raises(ValueError, match="pagination"):
        client._query("fixture query")


def test_cached_native_crops_are_verified_and_preserve_source_pixels(tmp_path: Path) -> None:
    path, product = safe_fixture(tmp_path)
    root = tmp_path / "cache" / UUID
    root.mkdir(parents=True)
    (root / "product.zip").write_bytes(path.read_bytes())
    token = Mock(
        side_effect=AssertionError("Cached verified reads must not request authentication")
    )
    client = CDSESentinel2Client(cache=tmp_path / "cache", token=token)
    x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(-52.5, 47.5)
    receipts = client.download_windows(
        client.item(product), (x - 50, y - 50, x + 50, y + 50), tmp_path / "chips"
    )
    for band in BANDS:
        receipt = receipts[band]
        with rasterio.open(tmp_path / "chips" / receipt["filename"]) as crop:
            values = crop.read(1)
            assert np.all(np.isin(values, [0, 6 if band == "SCL" else 1500]))
            assert crop.crs.to_epsg() == 32622
        assert receipt["sha256"] == file_digest(tmp_path / "chips" / receipt["filename"])
        assert receipt["source_resampling"] == "none"
        assert receipt["publisher_file_checksum"]["algorithm"] == "SHA3-256"
    token.assert_not_called()
    (root / "product.zip").write_bytes(b"changed cached data")
    with pytest.raises(ValueError):
        client.download_product(client.item(product))


def test_authenticated_atomic_download_checks_integrity_and_ignores_partial_cache(
    tmp_path: Path,
) -> None:
    path, product = safe_fixture(tmp_path)
    body = path.read_bytes()
    session = Mock(spec=requests.Session)
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [body[:10], body[10:]]
    session.get.return_value = response
    token = Mock(return_value="test-only-key")
    client = CDSESentinel2Client(
        session=cast(requests.Session, session), cache=tmp_path / "cache", token=token
    )
    root = tmp_path / "cache" / UUID
    root.mkdir(parents=True)
    (root / "product.zip.part").write_bytes(b"stale partial")
    archive, result = client.download_product(client.item(product))
    assert archive.read_bytes() == body and result["manifest_checksums_verified"] == 6
    assert not (root / "product.zip.part").exists()
    token.assert_called_once()
    assert session.get.call_args.kwargs["headers"] == {"Authorization": "Bearer test-only-key"}


def test_baseline_comparison_uses_declared_offsets_and_excludes_nodata(tmp_path: Path) -> None:
    a, b = tmp_path / "old.tif", tmp_path / "new.tif"
    for path, value in ((a, 500), (b, 1500)):
        values = np.full((2, 2), value, dtype=np.uint16)
        values[0, 0] = 0
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=2,
            height=2,
            count=1,
            dtype="uint16",
            crs="EPSG:32622",
            transform=from_origin(500000, 5300000, 10, 10),
            nodata=0,
        ) as raster:
            raster.write(values, 1)
    old = read_product_metadata(metadata(OLD, False), OLD)
    new = read_product_metadata(metadata(), NAME)
    result = compare_native_band(a, b, "B04", old, new)
    assert result["jointly_valid_pixels"] == 3
    assert result["raw_dn_delta_p05_median_p95"] == [1000, 1000, 1000]
    assert result["boa_reflectance_mae"] == 0
    with rasterio.open(b, "r+") as raster:
        raster.transform = from_origin(500010, 5300000, 10, 10)
    assert (
        compare_native_band(a, b, "B04", old, new)["comparison_status"]
        == "incompatible_grids_not_compared"
    )
