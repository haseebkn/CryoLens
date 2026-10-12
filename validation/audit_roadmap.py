"""Recheck existing real development evidence without touching reviewer submissions.

Run from the repository root. Raw products stay in the evidence checkout; all
new SNAP outputs and the audit report go into a new output directory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.ingest.sentinel2_safe import verify_safe_archive
from cryolens.preprocess.safe_reader import SAFEProductReader
from cryolens.preprocess.validation import (
    compare_window,
    require_development_product,
    run_reference,
    verify_reference_receipt,
)
from cryolens.reference.packet import read_workspace
from cryolens.reference.records import events
from cryolens.review.pairs import verify_pair_files


def audit(evidence: Path, output: Path, run_snap: bool = True) -> dict[str, Any]:
    if not __debug__:
        raise RuntimeError("Run without Python optimization: this audit requires its assertions")
    evidence = evidence.resolve()
    output.mkdir(parents=True, exist_ok=False)
    published = Path("docs/processing-validation/v1/report.json")
    original = json.loads(published.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "reviewed_stack_head": "e29986877035588f50dff47ecbdfc39df86a02f4",
        "published_processing_report_sha256": file_digest(published),
        "audit_script_sha256": file_digest(Path(__file__)),
        "processing_code_sha256": {
            p.name: file_digest(p) for p in Path("src/cryolens/preprocess").glob("*.py")
        },
        "fresh_snap_execution_requested": run_snap,
        "full_product_quality_rescanned": False,
        "sentinel1": [],
        "sentinel2": [],
        "optical_pairs": [],
        "reference_workspaces": [],
        "deployment_gate_open": False,
        "claim_boundary": "Development processing/integrity audit only. Cached full scans are source/code-bound historical measurements. No pilot images, human labels, detector tuning, accuracy metrics or scientific release are produced.",
    }

    def save() -> None:
        (output / "report.json").write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )

    save()
    for product in original["products"]:
        safe = evidence / "data/raw/safe" / product["product"]
        require_development_product(safe, Path("docs/evaluation/v1/manifest.json"))
        provenance = product["provenance"]
        assert file_digest(safe / "manifest.safe") == provenance["original_manifest_sha256"]
        for member, expected in provenance["annotation_sha256"].items():
            assert file_digest(safe / member) == expected
        for measurement in provenance["measurements"]:
            assert (
                file_digest(safe / "measurement" / measurement["measurement"])
                == measurement["source_sha256"]
            )
        for name in ("quality.py", "safe_reader.py"):
            historical = evidence / "src/cryolens/preprocess" / name
            current = Path("src/cryolens/preprocess") / name
            assert file_digest(historical) == original["validation_source_sha256"][name]
            assert current.read_bytes().replace(b"\r\n", b"\n") == historical.read_bytes().replace(
                b"\r\n", b"\n"
            )
        for channel in product["quality"]["channels"].values():
            assert (
                abs(
                    channel["nonpositive_fraction"]
                    - channel["nonpositive_pixels"] / channel["finite_pixels"]
                )
                < 1e-12
            )
        row: dict[str, Any] = {
            "product": product["product"],
            "mode": product["mode"],
            "source_hashes_verified": True,
            "historical_quality_code_and_sources_match": True,
            "code_comparison": "historical exact bytes match the published receipt; current quality/reader content matches after CRLF-to-LF normalization",
            "historical_quality_passed": product["quality"]["passed"],
            "historical_hv_nonpositive_fraction": product["quality"]["channels"]["HV"][
                "nonpositive_fraction"
            ],
            "windows": [],
        }
        report["sentinel1"].append(row)
        if run_snap:
            reader = SAFEProductReader(safe)
            scratch = evidence / "data/interim/safe-validation-setup/snap-input" / safe.name
            assert file_digest(scratch / "manifest.safe") == provenance["scratch_manifest_sha256"]
            old_receipts = Path("docs/processing-validation/v1/reference-receipts")
            for receipt_path in sorted(old_receipts.glob("*-receipt.json")):
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if (
                    receipt["scratch_files_sha256"].get("manifest.safe")
                    != provenance["scratch_manifest_sha256"]
                ):
                    continue
                for name, expected in receipt["scratch_files_sha256"].items():
                    assert file_digest(scratch / name) == expected
                window = tuple(receipt["window"])
                assert len(window) == 4
                prefix = output / "reference" / receipt_path.name.removesuffix("-receipt.json")
                graph = Path("configs/snap/validation-reference.xml")
                print("SNAP " + prefix.name, flush=True)
                run_reference(scratch, graph, prefix, window, original["reference_image"])
                verify_reference_receipt(
                    scratch, graph, prefix, window, original["reference_image"]
                )
                result = compare_window(reader, window, prefix, output / "diagnostics")
                result["reference_receipt_sha256"] = file_digest(
                    Path(str(prefix) + "-receipt.json")
                )
                row["windows"].append(result)
                save()
            assert len(row["windows"]) == 9
            row["scientific_comparison_passed"] = all(
                w["scientific_comparison_passed"] for w in row["windows"]
            )
        save()

    for folder in sorted((evidence / "data/raw/sentinel2-cdse").iterdir()):
        if not (folder / "product.zip").is_file():
            continue
        print("Verify CDSE " + folder.name, flush=True)
        product = json.loads((folder / "product.json").read_text(encoding="utf-8"))
        verified = verify_safe_archive(folder / "product.zip", product)
        previous = json.loads((folder / "verification.json").read_text(encoding="utf-8"))
        assert verified["archive_sha256"] == previous["archive_sha256"]
        report["sentinel2"].append(
            {
                k: verified[k]
                for k in (
                    "product_name",
                    "archive_sha256",
                    "archive_bytes",
                    "catalogue_checksums_verified",
                    "catalogue_checksum_algorithms_unchecked",
                    "zip_crc_verified",
                    "manifest_checksums_verified",
                )
            }
        )
        save()

    for pair_path in sorted((evidence / "data/processed/optical-review").glob("*/pair.json")):
        pair = json.loads(pair_path.read_text(encoding="utf-8"))
        verify_pair_files(pair_path.parent, pair)
        report["optical_pairs"].append(
            {
                "id": pair["id"],
                "manifest_sha256": file_digest(pair_path),
                "provider": pair.get("optical_provider", "planetary-computer"),
                "optical_item_id": pair["optical_item_id"],
                "files_verified": len(pair["files"]),
                "useful_for_review": pair["visibility"]["useful_for_review"],
            }
        )

    for name in ("development", "heldout"):
        root = evidence / "data/processed/reference/pilot-v1" / name
        workspace = read_workspace(root)
        # Read-only snapshot: never call ledger(), import, issue or freeze on live review data.
        with sqlite3.connect(
            (root / "annotations.sqlite3").as_uri() + "?mode=ro", uri=True
        ) as connection:
            history = events(connection)
        initial = [
            e
            for e in history
            if e["kind"] == "response" and e["body"]["response"]["phase"] == "initial"
        ]
        report["reference_workspaces"].append(
            {
                "name": name,
                "workspace_id": workspace["workspace_id"],
                "artifact_hashes_verified": True,
                "ledger_chain_verified": True,
                "observed_at": datetime.now(UTC).isoformat(),
                "ledger_head_sha256": history[-1]["sha256"] if history else None,
                "prepared_survey_tasks": workspace["survey_task_count"],
                "prepared_candidate_tasks": workspace["candidate_task_count"],
                "primary_human_responses": len(initial),
                "ai_review_events": sum(e["kind"] == "ai_assisted_visual_review" for e in history),
                "consistency_issues": sum(e["kind"] == "issue" for e in history),
                "freeze_events": sum(e["kind"] == "freeze" for e in history),
            }
        )
    report["completed_at"] = datetime.now(UTC).isoformat()
    save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-snap", action="store_true")
    args = parser.parse_args()
    result = audit(args.evidence_root, args.output, not args.skip_snap)
    print(
        json.dumps(
            {
                "completed": True,
                "snap_windows": sum(len(p["windows"]) for p in result["sentinel1"]),
                "s2_archives": len(result["sentinel2"]),
                "optical_pairs": len(result["optical_pairs"]),
                "deployment_gate_open": False,
            }
        )
    )
