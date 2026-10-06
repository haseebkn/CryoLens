"""Freeze actual adjudicated human references; never manufacture missing reviews."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from cryolens.eval.cohort import digest, file_digest
from cryolens.reference.packet import read_workspace, write_json
from cryolens.reference.records import append_event, events, ledger


def freeze_reference(
    root: Path, reviewer: str, adjudication: dict[str, Any], output: Path
) -> dict[str, Any]:
    """Choose recorded responses, require resolved review conflicts and persist exact coverage."""
    workspace = read_workspace(root)
    if not output.resolve().is_relative_to(root.resolve()) or output.exists():
        raise ValueError("Use a new snapshot folder inside this reference workspace")
    if (
        not adjudication.get("reviewer_id", "").strip()
        or len(adjudication.get("notes", "")) < 10
        or adjudication.get("target_ownership_at_core_boundaries_reviewed") is not True
    ):
        raise ValueError(
            "Explicit adjudicator, uncertainty notes and core-boundary ownership review are required"
        )
    date = datetime.fromisoformat(adjudication["adjudicated_utc"])
    if date.tzinfo is None or date > datetime.now(UTC):
        raise ValueError("Adjudication timestamp must be timezone-aware and not in the future")
    with ledger(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        history = events(connection)
        tasks = json.loads((root / "private/tasks.json").read_text())
        bodies = {e["key"]: e["body"] for e in history if e["kind"] == "response"}
        selected = {}
        for task in tasks:
            key = f"response:initial:{reviewer}:{task['task_id']}"
            if key not in bodies:
                raise ValueError(
                    "Every survey/candidate task needs an explicit primary reviewer disposition"
                )
            selected[task["task_id"]] = bodies[key]
        consistency_issues = [
            e["body"]
            for e in history
            if e["kind"] == "issue" and e["body"]["original_reviewer_id"] == reviewer
        ]
        if not consistency_issues:
            raise ValueError("No blinded repeat or independent reviewer consistency records")
        resolutions = {r["base_task_id"]: r for r in adjudication.get("case_resolutions", [])}
        if len(resolutions) != len(adjudication.get("case_resolutions", [])):
            raise ValueError("Duplicate adjudication case resolution")
        comparisons = []
        for issue in consistency_issues:
            for task in issue["tasks"]:
                key = f"response:{issue['phase']}:{issue['reviewer_id']}:{task['task_id']}"
                if key not in bodies:
                    raise ValueError("Complete all issued consistency tasks before freezing")
                first, second = bodies[task["first_response_key"]], bodies[key]

                def decisions(body: dict[str, Any]) -> dict[str, Any]:
                    return {
                        "status": body["response"]["status"],
                        "marks": body["response"]["marks"],
                        "excluded_regions": body["response"]["excluded_regions"],
                    }

                differs = decisions(first) != decisions(second)
                comparisons.append(
                    {
                        "base_task_id": task["base_task_id"],
                        "mode": issue["mode"],
                        "response_key": key,
                        "requires_adjudication": differs,
                    }
                )
                if differs and task["base_task_id"] not in resolutions:
                    raise ValueError(
                        "A differing review needs an explicit retained-response resolution"
                    )
        for identifier, resolution in resolutions.items():
            key = resolution.get("use_response_key")
            if (
                identifier not in selected
                or key not in bodies
                or bodies[key]["base_task_id"] != identifier
                or len(resolution.get("rationale", "")) < 10
            ):
                raise ValueError(
                    "Adjudication must choose a real response for the same task with rationale"
                )
            selected[identifier] = bodies[key]
        if any(
            b["mode"] == "candidate" and b["response"]["status"] != "complete"
            for b in selected.values()
        ):
            raise ValueError(
                "Sampled candidate nonresponse cannot be silently dropped from a frozen reference"
            )
        coverage = np.zeros(workspace["source_shape"], dtype=bool)
        eligible = np.zeros_like(coverage)
        annotations: list[dict[str, Any]] = []
        for task in tasks:
            body = selected[task["task_id"]]
            if task["mode"] != "survey":
                annotations.extend(
                    {
                        **a,
                        "reference_component": "candidate_review",
                        "candidate_id": task["candidate"]["id"],
                    }
                    for a in body["annotations"]
                )
                continue
            with np.load(root / task["grid"], allow_pickle=False) as grid:
                scored = grid["survey_domain"] & grid["core"]
                if body["response"]["status"] != "complete":
                    scored[:] = False
                for r0, c0, r1, c1 in body["response"]["excluded_regions"]:
                    scored[r0:r1, c0:c1] = False
                radius = workspace["policy"]["uncertain_buffer_pixels"]
                for mark in body["response"]["marks"]:
                    if mark["label"] == "uncertain":
                        row, col = mark["row"], mark["col"]
                        scored[
                            max(0, row - radius) : row + radius + 1,
                            max(0, col - radius) : col + radius + 1,
                        ] = False
                row, col = task["origin"]
                h, w = scored.shape
                coverage[row : row + h, col : col + w] |= scored
                eligible[row : row + h, col : col + w] |= grid["detector_eligible"] & grid["core"]
                for mark, annotation in zip(
                    body["response"]["marks"], body["annotations"], strict=True
                ):
                    annotations.append(
                        {
                            **annotation,
                            "point_id": digest(
                                {"task": task["task_id"], "row": mark["row"], "col": mark["col"]}
                            ),
                            "reference_component": "independent_area_search",
                            "inside_unambiguous_survey": bool(scored[mark["row"], mark["col"]]),
                            "inside_detection_eligible_survey": None
                            if workspace["detection"] is None
                            else bool(
                                scored[mark["row"], mark["col"]]
                                and grid["detector_eligible"][mark["row"], mark["col"]]
                            ),
                        }
                    )
        if not coverage.any():
            raise ValueError("No independently completed unambiguous survey coverage")
        if date < max(
            datetime.fromisoformat(b["response"]["reviewed_utc"]) for b in selected.values()
        ):
            raise ValueError("Adjudication predates an accepted review")
        independent = any(i["mode"] == "independent" for i in consistency_issues)
        expected_status = "obtained" if independent else "not_available"
        if adjudication.get("independent_reviewer_status") != expected_status:
            raise ValueError("Independent reviewer status must agree with the recorded responses")
        output.mkdir(parents=True)
        np.savez_compressed(
            output / "coverage.npz",
            independently_surveyed=coverage,
            detection_eligible=eligible,
            scored_detection_area=coverage & eligible,
        )
        write_json(output / "annotations.json", annotations)
        write_json(output / "adjudication.json", adjudication)
        payload = {
            "schema_version": 1,
            "workspace_id": workspace["workspace_id"],
            "parent_manifest_sha256": workspace["manifest_sha256"],
            "reference_protocol_sha256": workspace["policy_sha256"],
            "scene": workspace["scene"],
            "reviewer_id": reviewer,
            "frozen_at": datetime.now(UTC).isoformat(),
            "human_reference_records": len(annotations),
            "accepted_response_keys": [
                key
                for key, body in bodies.items()
                if any(body is selected_body for selected_body in selected.values())
            ],
            "review_consistency": comparisons,
            "independent_reviewer_obtained": independent,
            "surveyed_pixels": int(coverage.sum()),
            "detection_eligible_surveyed_pixels": int(np.count_nonzero(coverage & eligible)),
            "detection_eligibility_available": workspace["detection"] is not None,
            "approximate_surveyed_area_km2": float(coverage.sum())
            * workspace["nominal_pixel_area_km2"],
            "area_method": "exact saved native masks times nominal pixel spacing squared; approximate acquisition exposure",
            "reference_set_complete": True,
            "claim_boundary": "Adjudicated SAR observable-target references, not independent iceberg physical truth or operational accuracy",
            "files_sha256": {p.name: file_digest(p) for p in sorted(output.iterdir())},
        }
        write_json(output / "reference.json", {"payload": payload, "sha256": digest(payload)})
        append_event(
            connection,
            "freeze",
            "freeze:" + output.relative_to(root).as_posix(),
            {
                "folder": output.relative_to(root).as_posix(),
                "reference_sha256": file_digest(output / "reference.json"),
                "payload": payload,
            },
        )
    return payload
