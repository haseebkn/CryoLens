"""Source-bound sampled IW noise diagnostics; never a substitute for full-grid quality."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from cryolens.eval.cohort import file_digest
from cryolens.preprocess import safe_reader as reader_module
from cryolens.preprocess.provenance import safe_source_inventory
from cryolens.preprocess.safe_reader import SAFEProductReader, _parse_product_annotation
from cryolens.preprocess.validation import require_development_product


def summarize(raw: np.ndarray, corrected: np.ndarray) -> dict[str, Any]:
    if raw.shape != corrected.shape:
        raise ValueError("Raw and corrected grids must match")
    finite = np.isfinite(raw) & np.isfinite(corrected)
    noise = raw.astype(np.float64) - corrected.astype(np.float64)
    support = finite & (noise > 0)
    if np.any(noise[finite] < -1e-8):
        raise ValueError("Derived measured noise power is negative")
    count = int(finite.sum())
    ratio = raw[support] / noise[support]
    return {
        "finite_pixels": count,
        "nonpositive_fraction": float(np.count_nonzero(finite & (corrected <= 0)) / count)
        if count
        else None,
        "raw_to_annotated_noise_ratio_percentiles": [
            float(x) for x in np.percentile(ratio, [10, 50, 90])
        ]
        if ratio.size
        else None,
        "mean_signed_corrected_power": float(np.mean(corrected[finite], dtype=np.float64))
        if count
        else None,
        "mean_annotated_noise_power": float(np.mean(noise[finite])) if count else None,
        "noise_positive_support_pixels": int(support.sum()),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--safe", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a new diagnostic destination")
    require_development_product(args.safe, Path("docs/evaluation/v1/manifest.json"))
    reader = SAFEProductReader(args.safe)
    inputs = safe_source_inventory(reader)
    meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
    rows: list[dict[str, Any]] = []
    size = 512
    for rf in (0.1, 0.5, 0.9):
        for cf in (0.1, 0.5, 0.9):
            window = (
                round((meta["n_lines"] - size) * rf),
                round((meta["n_samples"] - size) * cf),
                size,
                size,
            )
            channels = {}
            for pol in ("HH", "HV"):
                raw = reader.read_sigma0(pol, remove_thermal_noise=False, window=window)
                corrected = reader.read_sigma0(pol, remove_thermal_noise=True, window=window)
                if raw["thermal_noise_removed"] or not corrected["thermal_noise_removed"]:
                    raise ValueError(
                        "Diagnostic requires a genuinely raw source and established subtraction"
                    )
                channels[pol] = summarize(raw["sigma0_linear"], corrected["sigma0_linear"])
                channels[pol]["incidence_median_deg"] = float(
                    np.nanmedian(corrected["incidence_angle_deg"])
                )
            rows.append({"window": list(window), "fractions": [rf, cf], "channels": channels})
    if safe_source_inventory(reader) != inputs:
        raise ValueError("SAFE source inventory changed during diagnostic")
    reader_file = reader_module.__file__
    if reader_file is None:
        raise RuntimeError("Reader module has no source provenance")
    report: dict[str, Any] = {
        "schema_version": 1,
        "product": reader.safe_dir.name,
        "source_files_sha256": inputs,
        "source_inventory_verified_after_scan": True,
        "driver_sha256": file_digest(Path(__file__)),
        "reader_sha256": file_digest(Path(reader_file)),
        "windows": rows,
        "claim_boundary": "Nine fixed source windows only. Ratio is measured raw backscatter divided by its annotated noise power; it is not an independent estimate of true signal or noise-model accuracy. No clipping, denoising, acceptance or performance measurement.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    for row in rows:
        hv = row["channels"]["HV"]
        print(
            row["fractions"],
            "HV non-positive",
            round(hv["nonpositive_fraction"] * 100, 3),
            "raw/noise median",
            hv["raw_to_annotated_noise_ratio_percentiles"][1],
            flush=True,
        )


if __name__ == "__main__":
    main()
