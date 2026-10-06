"""Verify original Sentinel-2 SAFE archives and their declared calibration metadata."""

from __future__ import annotations

import hashlib
import math
import re
import stat
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.ingest.sentinel2 import BANDS, utc

PRODUCT_NAME = re.compile(
    r"^(S2[ABC])_MSIL2A_(\d{8}T\d{6})_N(\d{4})_R(\d{3})_T(\d{2}[A-Z]{3})_(\d{8}T\d{6})\.SAFE$"
)
HASHES = {
    "MD5": "md5",
    "SHA1": "sha1",
    "SHA-1": "sha1",
    "SHA256": "sha256",
    "SHA-256": "sha256",
    "SHA3-256": "sha3_256",
}
BAND_IDS = {"B02": "1", "B03": "2", "B04": "3", "B08": "7"}


def product_identity(name: str) -> dict[str, str]:
    match = PRODUCT_NAME.fullmatch(name)
    if match is None:
        raise ValueError("Expected a compact Sentinel-2 L2A SAFE product name")
    platform, sensing, baseline, orbit, tile, generated = match.groups()
    return dict(
        platform=platform,
        sensing=sensing,
        baseline=baseline,
        orbit=orbit,
        tile=tile,
        generated=generated,
    )


def same_acquisition(a: str, b: str) -> bool:
    left, right = product_identity(a), product_identity(b)
    return all(left[k] == right[k] for k in ("platform", "sensing", "orbit", "tile"))


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _text(root: ET.Element, name: str) -> str:
    values = [e.text.strip() for e in root.iter() if _tag(e) == name and e.text]
    if len(values) != 1:
        raise ValueError("SAFE metadata needs one " + name)
    return values[0]


def _xml(data: bytes) -> ET.Element:
    if len(data) > 10 * 1024 * 1024 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Unsupported or oversized SAFE XML")
    return ET.fromstring(data)


def _relative(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe SAFE archive path")
    return str(path)


def read_product_metadata(data: bytes, name: str) -> dict[str, Any]:
    """Parse explicitly declared L2A encoding; missing modern offsets never default to zero."""
    identity = product_identity(name)
    metadata = _xml(data)
    if _text(metadata, "PRODUCT_URI") != name or _text(metadata, "PROCESSING_LEVEL") != "Level-2A":
        raise ValueError("SAFE metadata identity/processing level differs from catalogue")
    baseline = _text(metadata, "PROCESSING_BASELINE")
    if baseline.replace(".", "") != identity["baseline"]:
        raise ValueError("SAFE processing baseline differs from product name")
    acquisition = _text(metadata, "PRODUCT_START_TIME")
    if utc(acquisition).strftime("%Y%m%dT%H%M%S") != identity["sensing"]:
        raise ValueError("SAFE acquisition differs from product name")
    quantification = float(_text(metadata, "BOA_QUANTIFICATION_VALUE"))
    if not math.isfinite(quantification) or quantification <= 0:
        raise ValueError("Invalid BOA quantification")
    offsets = {
        e.attrib["band_id"]: float(e.text or "nan")
        for e in metadata.iter()
        if _tag(e) == "BOA_ADD_OFFSET"
    }
    band_offsets = {}
    for band, identifier in BAND_IDS.items():
        if int(identity["baseline"]) >= 400 and identifier not in offsets:
            raise ValueError("Modern SAFE processing requires declared BOA offsets")
        band_offsets[band] = offsets.get(identifier, 0.0)
        if not math.isfinite(band_offsets[band]):
            raise ValueError("Invalid BOA offset")
    special = {}
    for element in metadata.iter():
        if _tag(element) == "Special_Values":
            special[_text(element, "SPECIAL_VALUE_TEXT")] = int(
                _text(element, "SPECIAL_VALUE_INDEX")
            )
    if "NODATA" not in special:
        raise ValueError("SAFE product must declare nodata")
    return {
        "acquired_utc": acquisition,
        "processing_baseline": baseline,
        "boa_quantification_value": quantification,
        "boa_add_offsets_dn": band_offsets,
        "special_values": special,
    }


def verify_safe_archive(
    path: Path, product: dict[str, Any], max_bytes: int = 2 * 1024**3
) -> dict[str, Any]:
    """Check archive identity, catalogue hashes, ZIP CRCs and every manifest checksum."""
    name = product["Name"]
    identity = product_identity(name)
    if path.stat().st_size > max_bytes:
        raise ValueError("SAFE archive exceeds download budget")
    if product.get("ContentLength") and path.stat().st_size != int(product["ContentLength"]):
        raise ValueError("SAFE archive length differs from catalogue")
    verified_catalogue = []
    unsupported = []
    for checksum in product.get("Checksum", []):
        algorithm = checksum["Algorithm"].upper()
        if algorithm not in HASHES:
            unsupported.append(algorithm)
            continue
        hasher = hashlib.new(HASHES[algorithm], usedforsecurity=False)
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        if hasher.hexdigest().lower() != checksum["Value"].lower():
            raise ValueError("SAFE archive failed catalogue " + algorithm + " checksum")
        verified_catalogue.append({"algorithm": algorithm, "value": hasher.hexdigest()})
    with zipfile.ZipFile(path) as archive:
        members = {}
        seen = set()
        total_bytes = 0
        for info in archive.infolist():
            relative = _relative(info.filename)
            if PurePosixPath(relative).parts[0] != name:
                raise ValueError("SAFE archive contains a different product root")
            if relative.casefold() in seen or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("Duplicate or symbolic-link SAFE member")
            seen.add(relative.casefold())
            total_bytes += info.file_size
            if total_bytes > 4 * 1024**3:
                raise ValueError("SAFE archive uncompressed size exceeds budget")
            if not info.is_dir():
                members[relative] = info
        if archive.testzip() is not None:
            raise ValueError("SAFE archive failed ZIP CRC verification")
        manifest_name = name + "/manifest.safe"
        if manifest_name not in members or members[manifest_name].file_size > 10 * 1024 * 1024:
            raise ValueError("Missing or oversized SAFE manifest")
        manifest = _xml(archive.read(manifest_name))
        checks = {}
        for entry in manifest.iter():
            if _tag(entry) != "byteStream":
                continue
            locations = [e for e in entry if _tag(e) == "fileLocation"]
            hashes = [e for e in entry if _tag(e) == "checksum"]
            if len(locations) != 1 or len(hashes) != 1:
                raise ValueError("SAFE manifest file lacks an unambiguous checksum")
            member = name + "/" + _relative(locations[0].attrib["href"])
            algorithm = hashes[0].attrib["checksumName"].upper()
            if algorithm not in HASHES or not hashes[0].text or member not in members:
                raise ValueError("Missing SAFE member or unsupported manifest checksum")
            if member in checks:
                raise ValueError("Duplicate SAFE manifest file reference")
            if "size" in entry.attrib and members[member].file_size != int(entry.attrib["size"]):
                raise ValueError("SAFE member length differs from manifest")
            hasher = hashlib.new(HASHES[algorithm], usedforsecurity=False)
            with archive.open(member) as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
            if hasher.hexdigest().lower() != hashes[0].text.strip().lower():
                raise ValueError("SAFE member failed its publisher manifest checksum")
            checks[member] = {
                "algorithm": algorithm,
                "value": hasher.hexdigest(),
                "bytes": members[member].file_size,
            }
        if not checks:
            raise ValueError("SAFE manifest has no verifiable files")
        product_xml = name + "/MTD_MSIL2A.xml"
        if product_xml not in checks or members[product_xml].file_size > 10 * 1024 * 1024:
            raise ValueError("SAFE product metadata is not checksum-verified")
        metadata = read_product_metadata(archive.read(product_xml), name)
        if (
            abs(
                (
                    utc(metadata["acquired_utc"]) - utc(product["ContentDate"]["Start"])
                ).total_seconds()
            )
            > 1
        ):
            raise ValueError("SAFE acquisition differs from catalogue")
        bands = {}
        for band in BANDS:
            spacing = 20 if band == "SCL" else 10
            matches = [
                m
                for m in checks
                if f"/IMG_DATA/R{spacing}m/" in m and m.endswith(f"_{band}_{spacing}m.jp2")
            ]
            if len(matches) != 1 or f"_T{identity['tile']}_" not in matches[0]:
                raise ValueError("Missing, ambiguous or wrong-tile native SAFE band " + band)
            bands[band] = matches[0]
    return {
        "provider": "cdse",
        "product_uuid": product["Id"],
        "product_name": name,
        "archive_sha256": file_digest(path),
        "archive_bytes": path.stat().st_size,
        "catalogue_checksums_verified": verified_catalogue,
        "catalogue_checksum_algorithms_unchecked": unsupported,
        "zip_crc_verified": True,
        "manifest_checksums_verified": len(checks),
        "manifest_files": checks,
        "bands": bands,
        **metadata,
        "verification_scope": "whole delivered ZIP CRC and available catalogue hashes; every referenced SAFE manifest file checksum; product identity and metadata",
    }
