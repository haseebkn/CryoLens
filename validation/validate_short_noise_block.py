"""Verify the real short-block denoising regression and rescan its development product."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.quality import ProcessingQualityError, assess_safe_product
from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.validation import (
    compare_power,
    prepare_reference_input,
    read_reference,
    require_development_product,
    run_reference,
    verify_reference_receipt,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--safe", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--reference-prefix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-reference", action="store_true")
    args = parser.parse_args()
    source_hashes = {p.name: file_digest(p) for p in Path("src/cryolens/preprocess").glob("*.py")}
    driver_hash = file_digest(Path(__file__))
    require_development_product(args.safe, Path("docs/evaluation/v1/manifest.json"))
    args.output.mkdir(parents=True, exist_ok=False)
    provenance = prepare_reference_input(args.safe, args.scratch)
    window = (224, 4894, 99, 2078)
    image = "cryolens-snap-reference:14.0.0"
    graph = Path("configs/snap/validation-reference.xml")
    if args.run_reference:
        run_reference(args.scratch, graph, args.reference_prefix, window, image)
    verify_reference_receipt(args.scratch, graph, args.reference_prefix, window, image)
    reader = SAFEProductReader(args.safe)
    checks = {}
    for pol in ("HH", "HV"):
        checks["noise_" + pol] = compare_power(
            reader.read_sigma0(pol, window=window)["sigma0_linear"],
            read_reference(args.reference_prefix, "den", "sigma0_" + pol.lower()),
        )
        checks["calibration_" + pol] = compare_power(
            reader.read_sigma0(pol, False, window=window)["sigma0_linear"],
            read_reference(args.reference_prefix, "raw", "Sigma0_" + pol),
        )
    print("Short-block comparisons: " + json.dumps(checks), flush=True)
    try:
        quality = assess_safe_product(reader, args.output / "full-quality.json")
    except ProcessingQualityError as error:
        quality = error.report
    report = {
        "product": args.safe.name,
        "observed_at": datetime.now(UTC).isoformat(),
        "window": window,
        "source_provenance": provenance,
        "source_sha256": source_hashes,
        "driver_sha256": driver_hash,
        "reference_receipt_sha256": file_digest(Path(str(args.reference_prefix) + "-receipt.json")),
        "fresh_snap_run": args.run_reference,
        "signed_checks": checks,
        "full_quality": quality,
        "claim_boundary": "A real short azimuth block with no range knot. Calibration/noise comparison and fresh full quality scan only; no whole-product geometry acceptance or detector accuracy.",
    }
    (args.output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({"quality_passed": quality["passed"], "reasons": quality["reasons"]}))


if __name__ == "__main__":
    main()
