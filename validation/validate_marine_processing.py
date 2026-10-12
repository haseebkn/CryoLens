"""Fresh full quality scans and source-bound comparisons using verified SNAP caches.

Reads development products only. Write outputs to a new directory; original
products, reference caches, frozen partitions and reviewer workspaces are read-only.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.marine_geocoding import EGM96_ARCHIVE_SHA256
from cryolens.preprocess.quality import ProcessingQualityError, assess_safe_product
from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.validation import (
    compare_window,
    require_development_product,
    run_reference,
    verify_reference_receipt,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-reference", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    baseline = Path("docs/audits/pr6-9/report.json")
    original = json.loads(baseline.read_text())
    provenance = json.loads(Path("docs/processing-validation/v1/report.json").read_text())
    report = {
        "schema_version": 2,
        "started_at": datetime.now(UTC).isoformat(),
        "baseline_report_sha256": file_digest(baseline),
        "geoid_archive_sha256": EGM96_ARCHIVE_SHA256,
        "source_sha256": {
            p.name: file_digest(p) for p in Path("src/cryolens/preprocess").glob("*.py")
        },
        "driver_sha256": file_digest(Path(__file__)),
        "fresh_full_measurement_scans": True,
        "snap_cache_verified": True,
        "fresh_snap_runs_in_this_report": 0,
        "claim_boundary": "Development processing only. No held-out detection, model accuracy, reviewer annotations or fresh-detection release.",
        "products": [],
        "deployment_gate_open": False,
    }

    def save() -> None:
        (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))

    save()
    graph = Path("configs/snap/validation-reference-v2.xml")
    report["graph_sha256"] = file_digest(graph)
    fractions = json.loads(Path("configs/sentinel1-validation-v2.json").read_text())[
        "sampling_fractions"
    ]
    for previous, old in zip(original["sentinel1"], provenance["products"], strict=True):
        safe = args.evidence_root / "data/raw/safe" / previous["product"]
        require_development_product(safe, Path("docs/evaluation/v1/manifest.json"))
        reader = SAFEProductReader(safe)
        scratch = args.evidence_root / "data/interim/safe-validation-setup/snap-input" / safe.name
        name = (
            "old-ew"
            if safe.name.startswith("S1B")
            else "iw"
            if reader.instrument_mode == "IW"
            else "fresh-ew"
        )
        row = {
            "product": safe.name,
            "mode": reader.instrument_mode,
            "source_hashes_verified": False,
            "windows": [],
        }
        for member, expected in old["provenance"]["annotation_sha256"].items():
            if file_digest(safe / member) != expected:
                raise ValueError("Source annotation changed: " + member)
        if file_digest(safe / "manifest.safe") != old["provenance"]["original_manifest_sha256"]:
            raise ValueError("Source manifest changed")
        for measurement in old["provenance"]["measurements"]:
            if (
                file_digest(safe / "measurement" / measurement["measurement"])
                != measurement["source_sha256"]
            ):
                raise ValueError("Source measurement changed")
        row["source_hashes_verified"] = True
        report["products"].append(row)
        print("Fresh full quality scan: " + name, flush=True)
        try:
            row["quality"] = assess_safe_product(
                reader, args.output / (name + "-full-quality.json")
            )
        except ProcessingQualityError as error:
            row["quality"] = error.report
        save()
        for index, window_info in enumerate(previous["windows"]):
            window = tuple(window_info["window"])
            # Map baseline windows to their immutable named receipt.
            row_fraction, col_fraction = fractions[index]
            prefix = args.reference_root / f"{name}-r{row_fraction}-c{col_fraction}"
            receipt_path = Path(str(prefix) + "-receipt.json")
            if args.run_reference:
                run_reference(scratch, graph, prefix, window, provenance["reference_image"])
                report["fresh_snap_runs_in_this_report"] += 1
            verify_reference_receipt(
                scratch,
                graph,
                prefix,
                window,
                provenance["reference_image"],
            )
            print("Matched window: " + prefix.name, flush=True)
            result = compare_window(reader, window, prefix, args.output / "diagnostics")
            result["reference_receipt_sha256"] = file_digest(receipt_path)
            hv = reader.read_sigma0("HV", window=window)["sigma0_linear"]
            raw = reader.read_sigma0("HV", False, window=window)["sigma0_linear"]
            noise = raw - hv
            finite = np.isfinite(raw) & np.isfinite(noise) & (noise > 0)
            ratio = raw[finite].astype(np.float64) / noise[finite]
            result["noise_diagnostics_HV"] = {
                "pixels": int(finite.sum()),
                "raw_power_to_annotated_noise_ratio_quantiles": np.quantile(
                    ratio, [0.05, 0.5, 0.95]
                ).tolist(),
                "raw_power_below_annotated_noise_fraction": float(np.mean(ratio <= 1)),
                "signed_noise_reference_passed": result["checks"]["noise_HV"]["passed"],
                "interpretation": "Weak observed returns relative to annotated noise; this does not distinguish noise-model error from true low signal or justify clipping.",
            }
            row["windows"].append(result)
            save()
        row["scientific_comparison_passed"] = all(
            w["scientific_comparison_passed"] for w in row["windows"]
        )
        save()
    report["scientific_comparison_passed"] = all(
        p["scientific_comparison_passed"] for p in report["products"]
    )
    report["deployment_gate_open"] = report["scientific_comparison_passed"] and all(
        p["quality"]["passed"] for p in report["products"]
    )
    report["completed_at"] = datetime.now(UTC).isoformat()
    save()
    print(
        json.dumps(
            {
                "completed": True,
                "scientific_comparison_passed": report["scientific_comparison_passed"],
                "deployment_gate_open": report["deployment_gate_open"],
            }
        )
    )


if __name__ == "__main__":
    main()
