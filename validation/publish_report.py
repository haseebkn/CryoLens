"""Publish measured SAFE diagnostics without committing raw satellite rasters."""

import argparse
import json
import shutil
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--iw-receipt", type=Path, required=True)
    parser.add_argument("--nersc-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if len(report["products"]) != 3 or any(len(p["windows"]) != 9 for p in report["products"]):
        raise ValueError("Publish only the complete configured 27-window experiment")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.report, args.output_dir / "report.json")
    quality_reports = sorted(args.report.parent.glob("*-full-quality.json"))
    if len(quality_reports) != 3:
        raise ValueError("Expected the three retained full measurement scan reports")
    for path in quality_reports:
        measured = json.loads(path.read_text(encoding="utf-8"))
        expected = next(
            p["quality"] for p in report["products"] if p["product"] == measured["product"]
        )
        if measured != expected:
            raise ValueError("Full measurement scan differs from final comparison report")
        shutil.copyfile(path, args.output_dir / path.name)
    receipts = args.output_dir / "reference-receipts"
    receipts.mkdir(exist_ok=True)
    for path in sorted((args.report.parent / "reference").glob("*-receipt.json")):
        shutil.copyfile(path, receipts / path.name)
    receipt = json.loads(args.iw_receipt.read_text(encoding="utf-8"))
    receipt.pop("archive", None)
    (args.output_dir / "iw-integrity.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    shutil.copyfile(args.nersc_report, args.output_dir / "nersc-signed-power.json")
    figure, axes = plt.subplots(3, 2, figsize=(9, 10), constrained_layout=True)
    for row, product in enumerate(report["products"]):
        residual = np.array([w["nonpositive_fraction"]["HV"] * 100 for w in product["windows"]])
        errors = np.array(
            [
                max(
                    w["checks"][f"reprojection_{pol}"]["p95_absolute_error_db"]
                    for pol in ("HH", "HV")
                )
                for w in product["windows"]
            ]
        )
        for column, (values, title, upper, unit) in enumerate(
            [
                (residual, "HV non-positive power", 100, "%"),
                (errors, "Warp p95 absolute error vs SNAP", max(5, float(errors.max())), " dB"),
            ]
        ):
            axis = axes[row, column]
            axis.imshow(values.reshape(3, 3), vmin=0, vmax=upper, cmap="magma")
            for index, value in enumerate(values):
                axis.text(
                    index % 3,
                    index // 3,
                    f"{value:.2f}{unit}",
                    ha="center",
                    va="center",
                    color="white",
                )
            axis.set_xticks([0, 1, 2], ["10%", "50%", "90%"])
            axis.set_yticks([0, 1, 2], ["10%", "50%", "90%"])
            axis.set_xlabel("Source column position")
            axis.set_ylabel("Source row position")
            date = product["product"].split("_")[4][:8]
            axis.set_title(f"{product['mode']} {date}: {title}")
    state = "OPEN" if report["deployment_gate_open"] else "CLOSED"
    figure.suptitle(f"Real SAFE processing diagnostics — deployment gate {state}", fontsize=13)
    figure.savefig(args.output_dir / "spatial-diagnostics.png", dpi=160)
    plt.close(figure)


if __name__ == "__main__":
    main()
