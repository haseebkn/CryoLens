"""Download a predeclared CDSE batch and scan strict full-product radiometric quality."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cryolens.config.settings import Settings
from cryolens.eval.cohort import acquisition_id, file_digest, load_manifest
from cryolens.ingest.cdse import CDSEClient
from cryolens.preprocess.quality import ProcessingQualityError, QualityPolicy, assess_safe_product
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path


def guard(record: dict[str, Any], manifest: dict[str, Any]) -> None:
    start = datetime.fromisoformat(record["date"].replace("Z", "+00:00"))
    # Conservative exclusion: refuse every source within seven days of any test
    # source, even when its footprint does not overlap. No held-out bytes read.
    for source in manifest["scenes"]:
        if source["partition"] != "test":
            continue
        other = datetime.fromisoformat(source["acquired_utc"].replace("Z", "+00:00"))
        if (
            acquisition_id(record["name"]) == source["acquisition_id"]
            or abs((start - other).total_seconds()) <= 7 * 86400
        ):
            raise ValueError("Acquisition could contaminate frozen test group")


def verify_manifest(safe: Path) -> list[dict[str, Any]]:
    safe = Path(_open_path(safe))
    root = ET.parse(safe / "manifest.safe").getroot()
    checks: list[dict[str, Any]] = []
    for obj in root.iter():
        if obj.tag.split("}")[-1] != "dataObject":
            continue
        stream = next((x for x in obj if x.tag.split("}")[-1] == "byteStream"), None)
        if stream is None:
            continue
        loc = next((x for x in stream if x.tag.split("}")[-1] == "fileLocation"), None)
        if loc is None:
            continue
        member = (safe / loc.attrib["href"]).resolve()
        if not member.is_relative_to(safe.resolve()) or not member.is_file():
            raise ValueError("Manifest member absent or unsafe")
        if "size" in stream.attrib and member.stat().st_size != int(stream.attrib["size"]):
            raise ValueError("Manifest member size mismatch")
        check = next((x for x in stream if x.tag.split("}")[-1] == "checksum"), None)
        if check is not None:
            algo = check.attrib.get("checksumName", "").lower().replace("-", "")
            if algo not in {"md5", "sha256"}:
                raise ValueError("Unsupported manifest checksum")
            digest = hashlib.new(algo)
            with member.open("rb") as f:
                for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest().lower() != (check.text or "").strip().lower():
                raise ValueError("Manifest checksum mismatch: " + member.name)
            checks.append(
                {"member": member.relative_to(safe).as_posix(), "algorithm": algo, "matched": True}
            )
    if not any(c["member"].startswith("measurement/") for c in checks):
        raise ValueError("Manifest contains no verified measurement checksums")
    return checks


def load_quality_cache(quality: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Historical unbound scans cannot silently become current cached acceptance."""
    binding_path = quality.with_suffix(".binding.json")
    if not binding_path.is_file():
        raise ValueError("Quality cache lacks execution binding; use a new --quality-dir")
    binding = json.loads(binding_path.read_text())
    if binding.get("inputs_and_code") != expected or binding.get("report_sha256") != file_digest(
        quality
    ):
        raise ValueError("Quality cache inputs, code or report changed; use a new --quality-dir")
    result: dict[str, Any] = json.loads(quality.read_text())
    if (
        result.get("scope") != "full_measurement_grid"
        or result.get("policy") != QualityPolicy().model_dump()
    ):
        raise ValueError("Quality cache scope or policy differs from strict full scan")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument(
        "--quality-dir", type=Path, help="New directory for scans when historical cache is unbound"
    )
    args = p.parse_args()
    plan = json.loads(args.plan.read_text())
    manifest = load_manifest(Path(plan["frozen_manifest"]))
    args.root.mkdir(parents=True, exist_ok=True)
    settings_options: dict[str, Any] = {"_env_file": args.env_file}
    client = CDSEClient(cdse_settings=Settings(**settings_options).cdse)
    for idx, record in enumerate(plan["selected"]):
        guard(record, manifest)
        name = record["name"]
        if not re.fullmatch(r"S1[A-D]_(?:EW|IW)_[A-Za-z0-9_]+\.SAFE", name):
            raise ValueError("Invalid product name")
        product = args.root / "raw" / name.removesuffix(".SAFE")
        product.mkdir(parents=True, exist_ok=True)
        safe = Path(_open_path(product / name))
        receipt_path = product / "download-receipt.json"
        quality = (args.quality_dir or args.root / "quality") / (name + ".json")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            if receipt["catalogue"]["Id"] != record["id"] or receipt["catalogue"]["Name"] != name:
                raise ValueError("Cached download catalogue identity changed")
            for member, digest in receipt["safe_files_sha256"].items():
                if file_digest(safe / member) != digest:
                    raise ValueError("Previously downloaded file changed")
        else:
            if safe.exists():
                raise ValueError("Unreceipted SAFE already exists")
            required = 4 * record["bytes"] + 4 * 1024**3
            if shutil.disk_usage(args.root).free < required:
                raise ValueError(
                    "Insufficient free disk space for safe download/extraction/reference"
                )
            archive = product / (name + ".zip")
            partial = archive.with_suffix(".zip.part")
            url = (
                f"https://download.dataspace.copernicus.eu/odata/v1/Products({record['id']})/$value"
            )
            print("Downloading", idx + 1, "/", len(plan["selected"]), name, flush=True)
            digest = hashlib.sha256()
            md5 = hashlib.md5()
            total = 0
            last = 0.0
            if partial.exists() and zipfile.is_zipfile(partial):
                with partial.open("rb") as f:
                    for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                        digest.update(chunk)
                        md5.update(chunk)
                        total += len(chunk)
                length = None
                print("Reusing complete pending archive; integrity is checked again.", flush=True)
            else:
                with httpx.Client(
                    timeout=httpx.Timeout(600, connect=60), follow_redirects=True
                ) as http:
                    with http.stream(
                        "GET", url, headers={"Authorization": "Bearer " + client.get_auth_token()}
                    ) as response:
                        response.raise_for_status()
                        length = response.headers.get("Content-Length")
                        with partial.open("wb") as f:
                            for chunk in response.iter_bytes(1024 * 1024):
                                f.write(chunk)
                                digest.update(chunk)
                                md5.update(chunk)
                                total += len(chunk)
                                if time.monotonic() - last > 20:
                                    print("Transferred", round(total / 1e6, 1), "MB", flush=True)
                                    last = time.monotonic()
            if length is not None and total != int(length):
                raise ValueError("HTTP transfer length mismatch")
            expected_md5 = next(
                (
                    c["Value"]
                    for c in record["catalogue"].get("Checksum", [])
                    if c["Algorithm"].upper() == "MD5"
                ),
                None,
            )
            if expected_md5 is None or md5.hexdigest() != expected_md5.lower():
                raise ValueError("Archive does not match the CDSE catalogue MD5")
            if total != record["catalogue"]["ContentLength"]:
                raise ValueError("Archive does not match the CDSE catalogue byte count")
            if total < 1_000_000:
                raise ValueError("Archive unexpectedly small")
            with zipfile.ZipFile(partial) as z:
                if z.testzip() is not None:
                    raise ValueError("ZIP CRC mismatch")
                infos = z.infolist()
                for info in infos:
                    dest = (product / info.filename).resolve()
                    if (
                        not dest.is_relative_to(product.resolve())
                        or "\\" in info.filename
                        or ((info.external_attr >> 16) & 0o170000) == 0o120000
                    ):
                        raise ValueError("Unsafe ZIP member")
                with tempfile.TemporaryDirectory(prefix="extract-", dir=args.root) as temp:
                    z.extractall(_open_path(Path(temp)))
                    expected = Path(_open_path(Path(temp) / name))
                    if not (expected / "manifest.safe").is_file():
                        raise ValueError("Archive product identity mismatch")
                    expected.replace(safe)
            partial.replace(archive)
            verified = verify_manifest(safe)
            receipt = {
                "observed_at": datetime.now(UTC).isoformat(),
                "provider": "CDSE",
                "catalogue": record["catalogue"],
                "download_url": url,
                "http_content_length": length,
                "archive_bytes": total,
                "archive_sha256": digest.hexdigest(),
                "archive_md5": md5.hexdigest(),
                "catalogue_checksums_recorded_without_assuming_archive_semantics": record[
                    "catalogue"
                ].get("Checksum", []),
                "zip_crc_passed": True,
                "manifest_checks": verified,
                "plan_sha256": file_digest(args.plan),
                "frozen_manifest_sha256": file_digest(Path(plan["frozen_manifest"])),
                "purpose": "processing development only; excluded from frozen test groups",
                "safe_files_sha256": {
                    x.relative_to(safe).as_posix(): file_digest(x)
                    for x in sorted(safe.rglob("*"))
                    if x.is_file()
                },
            }
            receipt_path.write_text(json.dumps(receipt, indent=2, allow_nan=False))
            print("Downloaded and verified authentic SAFE:", name, "bytes", total, flush=True)
        quality_inputs = {
            "safe_files_sha256": receipt["safe_files_sha256"],
            "policy": QualityPolicy().model_dump(),
            "safe_reader_sha256": file_digest(Path("src/cryolens/preprocess/safe_reader.py")),
            "quality_code_sha256": file_digest(Path("src/cryolens/preprocess/quality.py")),
            "scan_block_rows": 32,
        }
        if not quality.exists():
            print("Full quality scan:", name, flush=True)
            try:
                result = assess_safe_product(SAFEProductReader(safe), quality, block_rows=32)
            except ProcessingQualityError as e:
                result = e.report
            quality.with_suffix(".binding.json").write_text(
                json.dumps(
                    {
                        "inputs_and_code": quality_inputs,
                        "report_sha256": file_digest(quality),
                    },
                    indent=2,
                    allow_nan=False,
                )
            )
        else:
            result = load_quality_cache(quality, quality_inputs)
        print(
            "Quality",
            name,
            json.dumps(
                {
                    "passed": result["passed"],
                    "channels": result["channels"],
                    "reasons": result["reasons"],
                }
            ),
            flush=True,
        )
    print("Batch complete; thresholds unchanged.", flush=True)


if __name__ == "__main__":
    main()
