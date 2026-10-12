"""Record a completed processing investigation, not a detector/model evaluation."""

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.eval.tracking import tracked_experiment
from cryolens.preprocess.validation import execution_artifact_bytes


@dataclass(frozen=True)
class ProcessingRecipe:
    path: Path
    configuration: dict[str, Any]
    recipe_id: str
    configuration_bytes: bytes
    graph_bytes: bytes


def processing_recipe(
    report: dict[str, Any], supplied: Path | None = None, snapshot: Path | None = None
) -> ProcessingRecipe:
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
            graph_bytes = execution_artifact_bytes(
                Path(configuration["graph"]),
                graph_hash,
                snapshot,
                report.get("execution_snapshot_sha256") if snapshot else None,
            )
        except ValueError:
            continue
        recipe = configuration.get("recipe_id", "v" + str(configuration["schema_version"]))
        matches.append(ProcessingRecipe(path, configuration, recipe, content, graph_bytes))
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
    selected = processing_recipe(report, args.config, snapshot)
    config_path, configuration, version = selected.path, selected.configuration, selected.recipe_id
    configuration_hash = hashlib.sha256(selected.configuration_bytes).hexdigest()
    graph_hash = hashlib.sha256(selected.graph_bytes).hexdigest()
    graph_path = Path(configuration["graph"])
    checkout_configuration = config_path.read_bytes() if config_path.is_file() else None
    checkout_graph = graph_path.read_bytes() if graph_path.is_file() else None
    checkout_hashes = {
        "configuration_sha256": (
            hashlib.sha256(checkout_configuration).hexdigest()
            if checkout_configuration is not None
            else None
        ),
        "graph_sha256": (
            hashlib.sha256(checkout_graph).hexdigest() if checkout_graph is not None else None
        ),
    }
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
    with (
        TemporaryDirectory(dir=Path.cwd(), prefix=".processing-recipe-") as recipe_dir,
        tracked_experiment(
            manifest_path=Path(configuration["frozen_manifest"]),
            project_root=Path.cwd(),
            data_root=Path("data"),
            task="target_detection",
            partition="scope",
            configuration={
                "purpose": "Post-hoc processing investigation; no detector performance evaluation",
                "configuration": configuration,
                "recipe_id": version,
                "recipe_configuration_sha256": configuration_hash,
                "recipe_graph_sha256": graph_hash,
                "recipe_configuration_binding": (
                    "recorded_hash"
                    if report.get("configuration_sha256")
                    else "post_hoc_graph_matched_checkout"
                ),
                "logging_checkout_recipe_sha256": checkout_hashes,
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
        ) as run,
    ):
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
                run.log_artifact(path, "logging_state/reference_recipes")
        logging_artifacts = Path(recipe_dir) / "logging"
        logging_artifacts.mkdir()
        for name, content in (
            (config_path.name, checkout_configuration),
            (graph_path.name, checkout_graph),
        ):
            if content is not None:
                artifact = logging_artifacts / name
                artifact.write_bytes(content)
                run.log_artifact(artifact, "logging_state/recipe")
        for name, content in (
            (config_path.name, selected.configuration_bytes),
            (graph_path.name, selected.graph_bytes),
        ):
            artifact = Path(recipe_dir) / name
            artifact.write_bytes(content)
            run.log_artifact(artifact, "reference_recipes")
        receipt = {
            "run_id": run.run_id,
            "experiment": experiment_name,
            "report_sha256": file_digest(report_path),
            "recipe_id": version,
            "configuration_sha256": configuration_hash,
            "graph_sha256": graph_hash,
            "logging_checkout_recipe_sha256": checkout_hashes,
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
