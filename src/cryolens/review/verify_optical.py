"""Direct SAFE verification and baseline-aware comparison with a stored optical pair."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import planetary_computer
import rasterio
import requests

from cryolens.config.settings import get_settings
from cryolens.eval.cohort import file_digest
from cryolens.ingest.sentinel2 import BANDS
from cryolens.ingest.sentinel2_cdse import CDSESentinel2Client
from cryolens.ingest.sentinel2_safe import read_product_metadata, same_acquisition
from cryolens.review.pairs import verify_pair_files


def compare_native_band(
    left: Path,
    right: Path,
    band: str,
    left_encoding: dict[str, Any],
    right_encoding: dict[str, Any],
) -> dict[str, Any]:
    """Compare matched native pixels only; incompatible grids are never silently resampled."""
    with rasterio.open(left) as first, rasterio.open(right) as second:
        same_grid = (
            first.crs == second.crs
            and first.transform == second.transform
            and first.shape == second.shape
        )
        result: dict[str, Any] = {
            "band": band,
            "same_native_grid": same_grid,
            "left_crs": str(first.crs),
            "right_crs": str(second.crs),
            "left_shape": list(first.shape),
            "right_shape": list(second.shape),
        }
        if not same_grid:
            result["comparison_status"] = "incompatible_grids_not_compared"
            return result
        a, b = first.read(1), second.read(1)
        amask, bmask = first.read_masks(1) > 0, second.read_masks(1) > 0
    if band == "SCL":
        declared_a = amask & ~np.isin(a, [0, 1])
        declared_b = bmask & ~np.isin(b, [0, 1])
    else:
        declared_a = amask & ~np.isin(a, list(left_encoding["special_values"].values()))
        declared_b = bmask & ~np.isin(b, list(right_encoding["special_values"].values()))
    valid = declared_a & declared_b
    count = int(valid.sum())
    result.update(
        native_window_pixels=int(a.size),
        jointly_valid_pixels=count,
        source_mask_agreement_fraction=float(np.mean(amask == bmask)),
        declared_validity_agreement_fraction=float(np.mean(declared_a == declared_b)),
        hosted_declared_valid_pixels=int(declared_a.sum()),
        cdse_declared_valid_pixels=int(declared_b.sum()),
        raw_arrays_identical=bool(np.array_equal(a, b)),
        comparison_status="compared" if count else "no_jointly_valid_pixels",
    )
    if not count:
        return result
    result["raw_value_equal_fraction_on_jointly_valid"] = float(np.mean(a[valid] == b[valid]))
    positions = np.flatnonzero(valid)[np.linspace(0, count - 1, min(10, count), dtype=int)]
    samples = []
    for index in positions:
        row, col = np.unravel_index(index, a.shape)
        samples.append(
            {
                "native_crop_row": int(row),
                "native_crop_col": int(col),
                "hosted_value": int(a[row, col]),
                "cdse_value": int(b[row, col]),
            }
        )
    result["matched_samples"] = samples
    if band == "SCL":
        pairs, counts = np.unique(
            np.stack([a[valid], b[valid]], axis=1), axis=0, return_counts=True
        )
        result["class_cross_counts"] = [
            {"hosted_class": int(pair[0]), "cdse_class": int(pair[1]), "pixels": int(n)}
            for pair, n in zip(pairs, counts, strict=True)
        ]
    else:
        delta = b[valid].astype(np.float64) - a[valid].astype(np.float64)
        first_boa = (
            a[valid].astype(np.float64) + left_encoding["boa_add_offsets_dn"][band]
        ) / left_encoding["boa_quantification_value"]
        second_boa = (
            b[valid].astype(np.float64) + right_encoding["boa_add_offsets_dn"][band]
        ) / right_encoding["boa_quantification_value"]
        boa_delta = second_boa - first_boa
        result.update(
            raw_dn_delta_p05_median_p95=np.quantile(delta, [0.05, 0.5, 0.95]).tolist(),
            boa_reflectance_delta_p05_median_p95=np.quantile(boa_delta, [0.05, 0.5, 0.95]).tolist(),
            boa_reflectance_mae=float(np.mean(np.abs(boa_delta))),
        )
    return result


def verify_reference(
    pair_path: Path, output: Path, client: CDSESentinel2Client | None = None
) -> dict[str, Any]:
    pair = json.loads(pair_path.read_text(encoding="utf-8"))
    if pair.get("optical_provider", "planetary-computer") != "planetary-computer" or not pair.get(
        "optical_item_id"
    ):
        raise ValueError("Comparison requires a real hosted optical pair")
    root = (get_settings().data_dir / "processed/optical-review" / pair["id"]).resolve()
    if not root.is_relative_to((get_settings().data_dir / "processed/optical-review").resolve()):
        raise ValueError("Invalid reference artifact path")
    verify_pair_files(root, pair)
    reference = next(
        item for item in pair["search"]["items"] if item["id"] == pair["optical_item_id"]
    )
    client = client or CDSESentinel2Client()
    match = client.match_product(reference)
    item = match["selected"]
    name = reference["properties"]["s2:product_uri"]
    if not same_acquisition(name, item["cdse_product"]["Name"]):
        raise ValueError("Comparison acquisitions/platform/orbit/tile differ")
    output.mkdir(parents=True, exist_ok=True)
    # Hosted metadata is a separate provenance source, not claimed as CDSE-verified.
    band_href = reference["assets"]["B04"]["href"]
    parsed = urlsplit(band_href)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".blob.core.windows.net")
        or ".SAFE/" not in band_href
    ):
        raise ValueError("Unexpected hosted metadata source")
    metadata_href = band_href.split(".SAFE/")[0] + ".SAFE/MTD_MSIL2A.xml"
    with requests.get(
        planetary_computer.sign_url(metadata_href), stream=True, timeout=60
    ) as response:
        if response.status_code != 200:
            raise RuntimeError("Hosted original metadata is unavailable")
        body = bytearray()
        for chunk in response.iter_content(64 * 1024):
            body.extend(chunk)
            if len(body) > 10 * 1024 * 1024:
                raise ValueError("Hosted metadata exceeds byte budget")
    (output / "hosted-product.xml").write_bytes(body)
    left_encoding = read_product_metadata(bytes(body), name)
    transform = pair["alignment"]["transform"]
    height, width = pair["alignment"]["shape"]
    bounds = (
        transform[2],
        transform[5] + height * transform[4],
        transform[2] + width * transform[0],
        transform[5],
    )
    assets = client.download_windows(item, bounds, output)
    verification = json.loads((output / "cdse-verification.json").read_text())
    results = [
        compare_native_band(
            root / f"{b}-native.tif", output / f"{b}-native.tif", b, left_encoding, verification
        )
        for b in BANDS
    ]
    report = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "reference_pair_id": pair["id"],
        "reference_manifest_sha256": file_digest(pair_path),
        "matching": match,
        "hosted_metadata": {
            "source_href": metadata_href,
            "sha256": file_digest(output / "hosted-product.xml"),
            **left_encoding,
        },
        "cdse_verification": verification,
        "cdse_native_assets": assets,
        "band_comparisons": results,
        "exact_product_equivalence_established": match["match_kind"] == "exact_product"
        and all(
            r.get("raw_arrays_identical") and r.get("source_mask_agreement_fraction") == 1
            for r in results
        ),
        "interpretation": "Same acquisition is not the same processing version. Native raw DN and SCL can change; BOA differences use each product's declared quantification and offsets. No target identity or radar verdict is inferred.",
    }
    (output / "comparison.json").write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("data/processed/cdse-optical-verification")
    )
    args = parser.parse_args()
    try:
        report = verify_reference(args.pair, args.output)
    except Exception as error:
        print(
            f"Direct optical verification failed ({type(error).__name__}); no equivalence result. Check provider access and local source artifacts. Credential-bearing request details are not printed."
        )
        return 1
    print("Direct SAFE verified:", report["cdse_verification"]["product_name"])
    print("Product match:", report["matching"]["match_kind"])
    print("Exact product equivalence established:", report["exact_product_equivalence_established"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
