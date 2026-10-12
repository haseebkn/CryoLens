"""Execute fixed SNAP references into fresh prefixes before source-bound comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path, _parse_product_annotation
from cryolens.preprocess.validation import (
    prepare_reference_input,
    require_development_product,
    run_reference,
    validate_configuration,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--scratch-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--image", default="cryolens-snap-reference:14.0.0")
    args = p.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("Fresh reference executions require a new output directory")
    config = json.loads(args.config.read_text())
    validate_configuration(config)
    args.output_dir.mkdir(parents=True)
    records = []
    for item in config["products"]:
        safe = Path(_open_path(Path(item["path"])))
        require_development_product(safe, Path(config["frozen_manifest"]))
        scratch = args.scratch_dir / safe.name
        provenance = prepare_reference_input(safe, scratch)
        reader = SAFEProductReader(safe)
        meta = _parse_product_annotation(reader._find_for_polarisation("HH")[1])
        size = config["window_size_px"]
        for rf, cf in config["sampling_fractions"]:
            window = (
                round((meta["n_lines"] - size) * rf),
                round((meta["n_samples"] - size) * cf),
                size,
                size,
            )
            prefix = args.output_dir / "reference" / f"{item['name']}-r{rf}-c{cf}"
            record = {"product": safe.name, "window": list(window), "prefix": prefix.name}
            print("Executing SNAP", safe.name, rf, cf, flush=True)
            try:
                run_reference(scratch, Path(config["graph"]), prefix, window, args.image)
                record["completed"] = True
                record["receipt_sha256"] = file_digest(Path(str(prefix) + "-receipt.json"))
            except (ValueError, RuntimeError, OSError) as error:
                record["completed"] = False
                record["reason"] = str(error)
            records.append(record)
            with (args.output_dir / (prefix.name + "-execution-summary.json")).open(
                "x", encoding="utf-8"
            ) as stream:
                json.dump(record, stream, indent=2)
            print("SNAP execution completed:", record["completed"], flush=True)
    report = {
        "schema_version": 1,
        "configuration_sha256": file_digest(args.config),
        "driver_sha256": file_digest(Path(__file__)),
        "reference_image": args.image,
        "provenance": provenance,
        "executions": records,
        "claim_boundary": "Actual reference execution inventory only; source-bound comparison, full quality and marine checks remain required. No scientific acceptance.",
    }
    with (args.output_dir / "reference-execution-inventory.json").open(
        "x", encoding="utf-8"
    ) as stream:
        json.dump(report, stream, indent=2)


if __name__ == "__main__":
    main()
