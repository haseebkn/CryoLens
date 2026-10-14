"""Adopt sealed, human-confirmed corrections as additive reference amendments."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from cryolens.eval.cohort import digest, file_digest
from cryolens.reference.coverage import uncertain_radius
from cryolens.reference.packet import read_workspace
from cryolens.reference.records import (
    HumanResponse,
    ManualRecheck,
    append_event,
    effective_primary_events,
    events,
    ledger,
    validate_response,
    verify_amendment_assets,
)


def adopt_corrections(
    root: Path,
    package_path: Path,
    receipt_path: Path,
    now: datetime | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate all rows before appending; never infer missing human attestations."""
    current = now or datetime.now(UTC)
    workspace = read_workspace(root)
    package_hash, receipt_hash = file_digest(package_path), file_digest(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if receipt.get("files_sha256", {}).get(package_path.name) != package_hash:
        raise ValueError("Correction package differs from its validation receipt")
    if package.get("workspace_id") != workspace["workspace_id"]:
        raise ValueError("Correction package belongs to another workspace")
    rows = package.get("corrections")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Correction package must contain explicit human responses")
    identifiers = [row["body"]["base_task_id"] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate task in correction package")
    tasks = {
        t["task_id"]: t
        for t in json.loads((root / "private/tasks.json").read_text(encoding="utf-8"))
    }
    confirmation = package.get("confirmation", {})
    folder = root / "corrections" / package_hash
    package_copy = folder / "corrections.json"
    receipt_copy = folder / "validation-receipt.json"
    provenance = {
        "package_sha256": package_hash,
        "receipt_sha256": receipt_hash,
        "files_sha256": {
            package_copy.relative_to(root).as_posix(): package_hash,
            receipt_copy.relative_to(root).as_posix(): receipt_hash,
        },
        "scope": "Human correction provenance; not independently verified field truth",
    }
    keys = [f"amendment:{package_hash}:{identifier}" for identifier in identifiers]
    with ledger(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        history = events(connection)
        verify_amendment_assets(root, history)
        existing = {e["key"]: e for e in history}
        if all(key in existing for key in keys):
            if any(existing[key]["body"]["amendment_provenance"] != provenance for key in keys):
                raise ValueError("Existing amendment provenance differs")
            return {"inserted_amendments": 0, "package_sha256": package_hash}
        if any(key in existing for key in keys):
            raise ValueError("Correction package was only partially adopted")
        if any(e["kind"] in {"issue", "freeze"} for e in history):
            raise ValueError(
                "Adopt corrections before consistency issuance or freezing; use a new reference version afterward"
            )
        if not history or package.get("primary_ledger_tip") != history[-1]["sha256"]:
            raise ValueError("Correction package is stale relative to the primary ledger")
        primary = {
            (e["body"]["response"]["reviewer_id"], e["body"]["base_task_id"]): e
            for e in effective_primary_events(history)
        }
        staged = []
        for row in rows:
            body = row["body"]
            manual = body.get("schema_variant") == "manual-recheck-v1"
            response = (
                ManualRecheck.model_validate(body["response"])
                if manual
                else HumanResponse.model_validate(body["response"])
            )
            if response.phase != "correction-proposal-v1":
                raise ValueError("Expected an explicit correction proposal")
            if response.task_id != body["base_task_id"] or response.task_id not in tasks:
                raise ValueError("Correction task identity differs")
            previous = primary.get((response.reviewer_id, response.task_id))
            if previous is None:
                raise ValueError("Correction has no prior response for this reviewer/task")
            previous_response = previous["body"]["response"]
            if body.get("supersedes_initial_response_sha256") != digest(previous_response):
                raise ValueError("Correction does not bind the active previous response")
            if response.reviewed_utc < datetime.fromisoformat(previous_response["reviewed_utc"]):
                raise ValueError("Correction predates its previous response")
            if manual:
                if (
                    confirmation.get("reviewer_id") != response.reviewer_id
                    or confirmation.get("personally_inspected") is not True
                    or confirmation.get("whole_valid_core_searched") is not True
                    or set(confirmation.get("viewed_channels", [])) != {"HH", "HV"}
                    or row["tile"] not in confirmation.get("tiles", [])
                    or len(confirmation.get("statement", "")) < 10
                    or response.reviewed_utc != datetime.fromisoformat(confirmation["received_utc"])
                ):
                    raise ValueError("Manual recheck requires the actual human confirmation")
            normalized = validate_response(
                root, workspace, response, tasks[response.task_id], None, current
            )
            if any(body.get(name) != value for name, value in normalized.items()):
                raise ValueError("Correction normalization differs from the source grid or policy")
            radius = uncertain_radius(body, workspace["policy"]["uncertain_buffer_pixels"])
            if body.get("position_is_approximate"):
                with np.load(root / tasks[response.task_id]["grid"], allow_pickle=False) as grid:
                    height, width = grid["survey_domain"].shape
                for mark in response.marks:
                    if mark.label != "uncertain" or mark.identity != "unknown":
                        raise ValueError(
                            "Approximate screenshot marks must remain uncertain/unknown"
                        )
                    required = [
                        max(0, mark.row - radius),
                        max(0, mark.col - radius),
                        min(height, mark.row + radius + 1),
                        min(width, mark.col + radius + 1),
                    ]
                    if not any(
                        r0 <= required[0]
                        and c0 <= required[1]
                        and r1 >= required[2]
                        and c1 >= required[3]
                        for r0, c0, r1, c1 in response.excluded_regions
                    ):
                        raise ValueError(
                            "Approximate position lacks its full uncertainty exclusion"
                        )
            staged.append(
                {
                    **body,
                    "previous_response_key": previous["key"],
                    "adopted_at": current.isoformat(),
                    "source_proposal_event_sha256": row["event_sha256"],
                    "amendment_provenance": provenance,
                }
            )
        if file_digest(package_path) != package_hash or file_digest(receipt_path) != receipt_hash:
            raise ValueError("Correction source changed during validation")
        if dry_run:
            return {
                "validated_amendments": len(staged),
                "inserted_amendments": 0,
                "package_sha256": package_hash,
                "dry_run": True,
            }
        folder.mkdir(parents=True, exist_ok=True)
        # Failed execution leaves reserved evidence; never overwrite prior bytes.
        for target, source, checksum in (
            (package_copy, package_path, package_hash),
            (receipt_copy, receipt_path, receipt_hash),
        ):
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(source.read_bytes())
            if file_digest(target) != checksum:
                raise ValueError("Reserved correction evidence differs")
        for key, body in zip(keys, staged, strict=True):
            append_event(connection, "reference_amendment", key, body)
        effective_primary_events(events(connection))
    return {
        "inserted_amendments": len(staged),
        "package_sha256": package_hash,
        "original_responses_overwritten": 0,
        "reference_frozen": False,
    }
