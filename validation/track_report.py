"""Record a completed processing investigation, not a detector/model evaluation."""

import argparse
import json
from pathlib import Path

from cryolens.eval.cohort import file_digest
from cryolens.eval.tracking import tracked_experiment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--tracking-uri", required=True)
    args = parser.parse_args()
    report_path = args.report_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    configuration = json.loads(Path("configs/sentinel1-validation-v1.json").read_text())
    with tracked_experiment(
        manifest_path=Path(configuration["frozen_manifest"]),
        project_root=Path.cwd(),
        data_root=Path("data"),
        task="target_detection",
        partition="scope",
        configuration={
            "purpose": "Post-hoc processing investigation; no detector performance evaluation",
            "configuration": configuration,
            "completed_report_sha256": file_digest(report_path),
            "actual_inputs": [p["provenance"] for p in report["products"]],
        },
        tracking_uri=args.tracking_uri,
        experiment_name="cryolens-processing-v1",
    ) as run:
        run.client.set_tag(run.run_id, "mlflow.runName", "sentinel1-processing-validation-v1")
        run.client.set_tag(run.run_id, "activity", "processing_validation_development_inputs")
        run.client.set_tag(
            run.run_id, "deployment_gate_open", str(report["deployment_gate_open"]).lower()
        )
        run.client.log_param(run.run_id, "validation_products", len(report["products"]))
        run.client.log_param(run.run_id, "reference_image_id", report["reference_image_id"])
        for index, product in enumerate(report["products"]):
            run.log_metrics(
                {
                    f"product_{index}_{pol}_nonpositive_fraction": product["quality"]["channels"][
                        pol
                    ]["nonpositive_fraction"]
                    for pol in ("HH", "HV")
                }
            )
        for path in sorted(args.report_dir.rglob("*")):
            if path.is_file() and path.name != "tracking_receipt.json":
                run.log_artifact(
                    path, "processing_report/" + path.parent.relative_to(args.report_dir).as_posix()
                )
        for path in sorted(Path("validation").iterdir()):
            if path.is_file():
                run.log_artifact(path, "reference_recipes")
        run.log_artifact(Path(configuration["graph"]), "reference_recipes")
        receipt = {
            "run_id": run.run_id,
            "experiment": "cryolens-processing-v1",
            "report_sha256": file_digest(report_path),
            "purpose": "Completed processing investigation; no precision, recall or false-alarm measurement",
        }
    (args.report_dir / "tracking_receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
