"""Record a completed processing investigation, not a detector/model evaluation."""

import argparse
import json
from pathlib import Path
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.eval.tracking import tracked_experiment
from cryolens.preprocess.validation import execution_artifact_bytes


def processing_recipe(
    report: dict[str, Any], supplied: Path | None = None, snapshot: Path | None = None
) -> tuple[Path, dict[str, Any], str]:
    """Select a recipe from bound execution evidence, independent of report schema."""
    graph_hash = report.get("graph_sha256")
    configuration_hash = report.get("configuration_sha256")
    if not graph_hash:
        raise ValueError("Processing report lacks bound graph provenance")
    candidates = (
        [supplied] if supplied else sorted(Path("configs").glob("sentinel1-validation-v*.json"))
    )
    matches = []
    for path in candidates:
        try:
            content = (
                execution_artifact_bytes(
                    path, configuration_hash, snapshot, report.get("execution_snapshot_sha256")
                )
                if configuration_hash
                else path.read_bytes()
            )
            configuration = json.loads(content)
            execution_artifact_bytes(
                Path(configuration["graph"]),
                graph_hash,
                snapshot,
                report.get("execution_snapshot_sha256") if snapshot else None,
            )
        except ValueError:
            continue
        recipe = configuration.get("recipe_id", "v" + str(configuration["schema_version"]))
        matches.append((path, configuration, recipe))
    if len(matches) != 1:
        raise ValueError(
            "Processing report configuration/graph provenance conflicts or is ambiguous"
        )
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--tracking-uri", required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    report_path = args.report_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    snapshot = (
        args.report_dir / "executed-code.zip" if report.get("execution_snapshot_sha256") else None
    )
    config_path, configuration, version = processing_recipe(report, args.config, snapshot)
    if (args.report_dir / "tracking_receipt.json").exists():
        raise FileExistsError("Preserve existing tracking evidence; use a new report directory")
    experiment_name = "cryolens-processing-" + version
    receipts = {
        file_digest(p): json.loads(p.read_text())
        for p in (args.report_dir / "reference-receipts").glob("*-receipt.json")
    }
    required = {
        window["reference_receipt_sha256"]
        for product in report["products"]
        for window in product.get("windows", [])
        if "reference_receipt_sha256" in window
    }
    required.update(
        product["provenance"]["reference_receipt_sha256"]
        for product in report["products"]
        if "reference_receipt_sha256" in product.get("provenance", {})
    )
    if required and not required <= receipts.keys():
        raise ValueError("Processing report references missing or changed receipts")
    selected_receipts = [receipts[key] for key in required] if required else list(receipts.values())
    if any(receipt.get("graph_sha256") != report["graph_sha256"] for receipt in selected_receipts):
        raise ValueError("Processing report and reference graph provenance conflict")
    image_ids = {receipt["image_id"] for receipt in selected_receipts}
    if len(image_ids) != 1:
        raise ValueError("Processing tracking needs one consistent verified reference runtime")
    if report.get("reference_image_id", next(iter(image_ids))) != next(iter(image_ids)):
        raise ValueError("Processing report and reference runtime provenance conflict")
    with tracked_experiment(
        manifest_path=Path(configuration["frozen_manifest"]),
        project_root=Path.cwd(),
        data_root=Path("data"),
        task="target_detection",
        partition="scope",
        configuration={
            "purpose": "Post-hoc processing investigation; no detector performance evaluation",
            "configuration": configuration,
            "recipe_id": version,
            "recipe_configuration_sha256": file_digest(config_path),
            "recipe_graph_sha256": file_digest(Path(configuration["graph"])),
            "completed_report_sha256": file_digest(report_path),
            "actual_inputs": [
                p["provenance"]
                if "provenance" in p
                else {
                    "product": p["product"],
                    "mode": p["mode"],
                    "source_hashes_verified": p["source_hashes_verified"],
                    "baseline_report_sha256": report["baseline_report_sha256"],
                }
                for p in report["products"]
            ],
            "activity": "Post-hoc logging of completed processing evidence; generation source is separately preserved in the executed-code artifact",
        },
        tracking_uri=args.tracking_uri,
        experiment_name=experiment_name,
    ) as run:
        run.client.set_tag(
            run.run_id, "mlflow.runName", "sentinel1-processing-validation-" + version
        )
        run.client.set_tag(run.run_id, "activity", "processing_validation_development_inputs")
        run.client.set_tag(
            run.run_id, "deployment_gate_open", str(report["deployment_gate_open"]).lower()
        )
        run.client.log_param(run.run_id, "validation_products", len(report["products"]))
        run.client.log_param(run.run_id, "reference_image_id", next(iter(image_ids)))
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
                relative = path.parent.relative_to(args.report_dir).as_posix()
                run.log_artifact(
                    path,
                    "processing_report" if relative == "." else "processing_report/" + relative,
                )
        for path in sorted(Path("validation").iterdir()):
            if path.is_file():
                run.log_artifact(path, "reference_recipes")
        if snapshot is None:
            run.log_artifact(Path(configuration["graph"]), "reference_recipes")
        receipt = {
            "run_id": run.run_id,
            "experiment": experiment_name,
            "report_sha256": file_digest(report_path),
            "recipe_id": version,
            "configuration_sha256": report.get("configuration_sha256") or file_digest(config_path),
            "graph_sha256": report["graph_sha256"],
            "purpose": "Completed processing investigation; no precision, recall or false-alarm measurement",
            "git_commit": run.client.get_run(run.run_id).data.tags["git_commit"],
            "git_dirty": run.client.get_run(run.run_id).data.tags["git_dirty"],
        }
    (args.report_dir / "tracking_receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
