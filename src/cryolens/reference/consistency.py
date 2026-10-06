"""Issued concealed-answer repeat packets and paired reviewer agreement."""

from __future__ import annotations

import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from cryolens.eval.annotations import ReferencePoint, match_targets
from cryolens.eval.cohort import digest, file_digest
from cryolens.reference.packet import install_viewer, read_workspace
from cryolens.reference.records import append_event, events, ledger, phase_tasks


def issue_review(
    root: Path,
    original_reviewer: str,
    reviewer: str,
    mode: str = "repeat",
    fraction: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    workspace = read_workspace(root)
    if (
        mode not in {"repeat", "independent"}
        or not original_reviewer.strip()
        or not reviewer.strip()
    ):
        raise ValueError("Specify valid reviewer IDs and repeat/independent mode")
    if (mode == "repeat") != (reviewer == original_reviewer):
        raise ValueError(
            "Repeats use the same reviewer; independent review needs a different reviewer"
        )
    fraction = (
        fraction
        if fraction is not None
        else workspace["policy"]["repeat_fraction"]
        if mode == "repeat"
        else 1.0
    )
    if not np.isfinite(fraction) or not 0 < fraction <= 1:
        raise ValueError("Review fraction must be in (0, 1]")
    current = now or datetime.now(UTC)
    with ledger(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        history = events(connection)
        initial = [
            e
            for e in history
            if e["kind"] == "response"
            and e["body"]["response"]["phase"] == "initial"
            and e["body"]["response"]["reviewer_id"] == original_reviewer
        ]
        if not initial:
            raise ValueError("No personally inspected initial responses are available")
        original_tasks, _ = phase_tasks(root, history, "initial")
        if mode == "repeat" and {t["task_id"] for t in original_tasks} != {
            e["body"]["base_task_id"] for e in initial
        }:
            raise ValueError(
                "Disposition every primary pilot task before selecting the random repeat subset"
            )
        latest = max(datetime.fromisoformat(e["body"]["response"]["reviewed_utc"]) for e in initial)
        not_before = (
            latest + timedelta(days=workspace["policy"]["repeat_delay_days"])
            if mode == "repeat"
            else current
        )
        if current < not_before:
            raise ValueError("Blind re-review is not due until " + not_before.isoformat())
        phase = digest(
            {
                "mode": mode,
                "original": original_reviewer,
                "reviewer": reviewer,
                "fraction": fraction,
                "responses": sorted(e["sha256"] for e in initial),
                "seed": workspace["policy"]["seed"],
            }
        )[:24]
        existing: dict[str, Any] | None = next(
            (e["body"] for e in history if e["key"] == "issue:" + phase), None
        )
        if existing:
            phase_tasks(root, history, phase)
            return existing
        ordered = sorted(initial, key=lambda e: e["key"])
        count = max(1, math.ceil(len(ordered) * fraction))
        rng = np.random.default_rng(
            int(digest({"phase": phase, "seed": workspace["policy"]["seed"]})[:16], 16)
        )
        chosen = [ordered[int(i)] for i in rng.choice(len(ordered), count, replace=False)]
        by_id = {t["task_id"]: t for t in original_tasks}
        folder = root / "review" / phase
        folder.mkdir(parents=True, exist_ok=False)
        public, private = [], []
        for index, event in enumerate(chosen):
            original_id = event["body"]["base_task_id"]
            task = by_id[original_id]
            source_folder = (
                root / "review" / ("survey" if task["mode"] == "survey" else "candidates")
            )
            import json

            source_packet = json.loads((source_folder / "tasks.json").read_text())
            source_public = next(t for t in source_packet["tasks"] if t["task_id"] == original_id)
            new_id = digest({"phase": phase, "index": index, "base": original_id})[:32]
            copied = {**source_public, "task_id": new_id, "optical": []}
            for band in ("HH", "HV"):
                name = f"{new_id}-{band}.png"
                shutil.copyfile(source_folder / source_public[band], folder / name)
                copied[band] = name
            for optical_index, optical in enumerate(source_public["optical"]):
                name = f"{new_id}-optical-{optical_index}.png"
                shutil.copyfile(source_folder / optical["image"], folder / name)
                copied["optical"].append({**optical, "image": name})
            public.append(copied)
            private.append(
                {
                    **task,
                    "task_id": new_id,
                    "base_task_id": original_id,
                    "first_response_key": event["key"],
                }
            )
        install_viewer(
            folder,
            public,
            {
                "workspace_id": workspace["workspace_id"],
                "protocol_id": workspace["policy"]["protocol_id"],
                "phase": phase,
                "assignment": reviewer,
                "not_before_utc": not_before.isoformat(),
                "queue": "Independent review"
                if mode == "independent"
                else "Concealed-answer repeat review",
                "identity_claim_boundary": "Previous answers and detector status are withheld. Recognizing an image is still possible.",
            },
        )
        hashes = {
            p.relative_to(root).as_posix(): file_digest(p)
            for p in sorted(folder.rglob("*"))
            if p.is_file()
        }
        issue = {
            "phase": phase,
            "mode": mode,
            "original_reviewer_id": original_reviewer,
            "reviewer_id": reviewer,
            "not_before_utc": not_before.isoformat(),
            "issued_at": current.isoformat(),
            "population_count": len(ordered),
            "selected_count": count,
            "repeat_inclusion_probability": count / len(ordered),
            "selection_method": "seeded_simple_random_without_replacement",
            "folder": folder.relative_to(root).as_posix(),
            "tasks": private,
            "files_sha256": hashes,
        }
        append_event(connection, "issue", "issue:" + phase, issue)
    return issue


def agreement(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candidate label agreement/kappa and surveyed-tile target correspondence, never accuracy."""
    by_key = {e["key"]: e["body"] for e in history if e["kind"] == "response"}
    reports = []
    for issue_event in (e for e in history if e["kind"] == "issue"):
        issue = issue_event["body"]
        matrix = {
            a: {b: 0 for b in ("target", "non_target", "uncertain")}
            for a in ("target", "non_target", "uncertain")
        }
        paired = 0
        surveys = []
        for task in issue["tasks"]:
            first = by_key[task["first_response_key"]]
            key = f"response:{issue['phase']}:{issue['reviewer_id']}:{task['task_id']}"
            second = by_key.get(key)
            if second is None:
                continue
            paired += 1
            if task["mode"] == "candidate":
                if first["response"]["status"] == second["response"]["status"] == "complete":
                    a = first["response"]["marks"][0]["label"]
                    b = second["response"]["marks"][0]["label"]
                    matrix[a][b] += 1
            else:

                def points(
                    body: dict[str, Any], prefix: str, scene_id: str
                ) -> list[ReferencePoint]:
                    return [
                        ReferencePoint(
                            point_id=f"{prefix}-{i}",
                            scene_id=scene_id,
                            longitude=a["target_detection"]["longitude"],
                            latitude=a["target_detection"]["latitude"],
                        )
                        for i, a in enumerate(body["annotations"])
                        if a["target_detection"]["label"] == "target"
                    ]

                left, right = (
                    points(first, "first", task["scene_id"]),
                    points(second, "second", task["scene_id"]),
                )
                surveys.append(
                    {
                        "base_task_id": task["base_task_id"],
                        "first_status": first["response"]["status"],
                        "second_status": second["response"]["status"],
                        "first_targets": len(left),
                        "second_targets": len(right),
                        "matched_targets_200m": len(match_targets(left, right, 200)),
                        "identity_accuracy_measured": False,
                    }
                )
        count = sum(sum(row.values()) for row in matrix.values())
        observed = sum(matrix[label][label] for label in matrix) / count if count else None
        expected = (
            sum(
                sum(matrix[label].values()) * sum(matrix[row][label] for row in matrix)
                for label in matrix
            )
            / count**2
            if count
            else None
        )
        kappa = (
            (observed - expected) / (1 - expected)
            if observed is not None and expected is not None and expected < 1
            else None
        )
        reports.append(
            {
                "mode": issue["mode"],
                "original_reviewer": issue["original_reviewer_id"],
                "reviewer": issue["reviewer_id"],
                "issued_tasks": issue["selected_count"],
                "paired_responses": paired,
                "candidate_paired_resolved_or_uncertain": count,
                "candidate_label_confusion": matrix,
                "candidate_exact_agreement": observed,
                "candidate_cohen_kappa": kappa,
                "survey_target_correspondence": surveys,
                "independent_review": issue["mode"] == "independent",
                "agreement_is_accuracy": False,
            }
        )
    return reports
