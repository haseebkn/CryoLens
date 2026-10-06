"""Validated human responses in an append-only, hash-chained local ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Geod

from cryolens.eval.annotations import Annotation, Evidence
from cryolens.eval.cohort import digest, file_digest
from cryolens.reference.packet import read_workspace


class SupportingObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["optical", "ais", "field_observation"]
    reference: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    acquired_utc: datetime
    rationale: str = Field(min_length=10)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    matching_radius_m: float = Field(gt=0, allow_inf_nan=False)
    position_uncertainty_m: float = Field(ge=0, allow_inf_nan=False)
    positive_observation: Literal[True]
    visibility: Literal["clear", "uncertain", "unavailable"] | None = None
    valid_optical_pixels: bool | None = None
    resolution_m: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def usable(self) -> Self:
        if self.acquired_utc.tzinfo is None:
            raise ValueError("Supporting observation needs a timezone")
        if self.kind == "optical" and (
            self.visibility != "clear"
            or self.valid_optical_pixels is not True
            or self.resolution_m is None
        ):
            raise ValueError(
                "Optical identity needs inspected clear, valid pixels and recorded resolution"
            )
        return self


class PixelMark(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    label: Literal["target", "non_target", "uncertain"]
    identity: Literal["unknown", "ship", "iceberg", "other"] = "unknown"
    rationale: str = Field(min_length=3)
    supporting_observations: list[SupportingObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def identity_supported(self) -> Self:
        if self.identity != "unknown" and (
            self.label != "target" or not self.supporting_observations
        ):
            raise ValueError(
                "Resolved identity requires a target and positive independent evidence"
            )
        return self


class HumanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    phase: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    reviewed_utc: datetime
    status: Literal["complete", "partial", "unusable"]
    active_seconds: float = Field(gt=0, le=24 * 3600, allow_inf_nan=False)
    personally_inspected: Literal[True]
    viewed_channels: list[Literal["HH", "HV"]]
    notes: str = Field(min_length=3)
    marks: list[PixelMark] = Field(default_factory=list)
    excluded_regions: list[tuple[int, int, int, int]] = Field(default_factory=list)

    @model_validator(mode="after")
    def aware(self) -> Self:
        if self.reviewed_utc.tzinfo is None or not self.reviewer_id.strip():
            raise ValueError("Reviewer identity and timezone-aware review time are required")
        if len({(m.row, m.col) for m in self.marks}) != len(self.marks):
            raise ValueError("Duplicate marked pixel")
        if self.status == "complete" and set(self.viewed_channels) != {"HH", "HV"}:
            raise ValueError("Complete SAR inspection requires both polarizations")
        return self


def ledger(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "annotations.sqlite3")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            event_key TEXT UNIQUE NOT NULL,
            body TEXT NOT NULL,
            previous_sha256 TEXT NOT NULL,
            event_sha256 TEXT NOT NULL UNIQUE
        );
        CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'Reference history is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'Reference history is append-only'); END;
    """)
    return connection


def events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    previous = "0" * 64
    result: list[dict[str, Any]] = []
    for sequence, kind, key, encoded, prior, checksum in connection.execute(
        "SELECT sequence, kind, event_key, body, previous_sha256, event_sha256 FROM events ORDER BY sequence"
    ):
        body = json.loads(encoded)
        expected = digest({"kind": kind, "key": key, "body": body, "previous": previous})
        if sequence != len(result) + 1 or prior != previous or checksum != expected:
            raise ValueError("Reference ledger hash chain changed")
        result.append({"kind": kind, "key": key, "body": body, "sha256": checksum})
        previous = checksum
    return result


def append_event(connection: sqlite3.Connection, kind: str, key: str, body: dict[str, Any]) -> bool:
    history = events(connection)
    existing = next((event for event in history if event["key"] == key), None)
    if existing is not None:
        if existing["body"] != body or existing["kind"] != kind:
            raise ValueError(
                "Conflicting annotation/issue already exists; history cannot be overwritten"
            )
        return False
    previous = history[-1]["sha256"] if history else "0" * 64
    checksum = digest({"kind": kind, "key": key, "body": body, "previous": previous})
    connection.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
        (
            len(history) + 1,
            kind,
            key,
            json.dumps(body, sort_keys=True, allow_nan=False),
            previous,
            checksum,
        ),
    )
    return True


def phase_tasks(
    root: Path, history: list[dict[str, Any]], phase: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if phase == "initial":
        return json.loads((root / "private/tasks.json").read_text()), None
    issue = next(
        (
            event["body"]
            for event in history
            if event["kind"] == "issue" and event["body"]["phase"] == phase
        ),
        None,
    )
    if issue is None:
        raise ValueError("Unissued review phase")
    for name, expected in issue["files_sha256"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or file_digest(path) != expected:
            raise ValueError("Issued blinded packet changed")
    return issue["tasks"], issue


def completed_survey(
    history: list[dict[str, Any]], tasks: list[dict[str, Any]], reviewer: str
) -> bool:
    survey_ids = {t["task_id"] for t in tasks if t["mode"] == "survey"}
    responded = {
        e["body"]["response"]["task_id"]
        for e in history
        if e["kind"] == "response"
        and e["body"]["response"]["phase"] == "initial"
        and e["body"]["response"]["reviewer_id"] == reviewer
    }
    # All tasks need a disposition. Partial/unusable coverage stays explicitly unscored.
    return bool(survey_ids) and survey_ids <= responded


def validate_response(
    root: Path,
    workspace: dict[str, Any],
    response: HumanResponse,
    task: dict[str, Any],
    issue: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    if response.workspace_id != workspace["workspace_id"]:
        raise ValueError("Response belongs to another workspace")
    if response.reviewed_utc > now or response.reviewed_utc < datetime.fromisoformat(
        workspace["created_at"]
    ):
        raise ValueError("Review time is outside the prepared/current interval")
    if issue is not None:
        if response.reviewer_id != issue[
            "reviewer_id"
        ] or response.reviewed_utc < datetime.fromisoformat(issue["not_before_utc"]):
            raise ValueError("Assigned reviewer or blinded re-review date does not match")
    with np.load(root / task["grid"], allow_pickle=False) as grid:
        domain = grid["survey_domain"] & grid["core"]
        latitude, longitude = grid["latitude"], grid["longitude"]
        excluded = np.zeros_like(domain)
        h, w = domain.shape
        for r0, c0, r1, c1 in response.excluded_regions:
            if not (0 <= r0 < r1 <= h and 0 <= c0 < c1 <= w):
                raise ValueError("Excluded region is outside the review grid")
            excluded[r0:r1, c0:c1] = True
        if task["mode"] == "candidate" and (
            response.status == "complete" and len(response.marks) != 1
        ):
            raise ValueError("Complete candidate response requires exactly one explicit label")
        normalized = []
        for mark in response.marks:
            if mark.row >= h or mark.col >= w or not domain[mark.row, mark.col]:
                raise ValueError("Marked position is not a valid NL offshore core pixel")
            if task["mode"] == "candidate":
                # Candidate position belongs to the immutable sampled component, not a nearby object.
                if (mark.row, mark.col) != (
                    task["row"] - task["origin"][0],
                    task["col"] - task["origin"][1],
                ):
                    raise ValueError("Candidate assessment must use its displayed centre")
            elif mark.label == "non_target":
                raise ValueError(
                    "Independent survey records targets/uncertainties; background is established by completed coverage"
                )
            lon, lat = float(longitude[mark.row, mark.col]), float(latitude[mark.row, mark.col])
            sar_evidence = Evidence(
                kind="sar_review",
                reference=f"{workspace['workspace_id']}/{task.get('base_task_id', task['task_id'])}/HH-HV",
                acquired_utc=task["acquired_utc"],
                rationale=mark.rationale,
            )
            evidence = [sar_evidence]
            for observation in mark.supporting_observations:
                if (
                    abs(
                        (
                            observation.acquired_utc - datetime.fromisoformat(task["acquired_utc"])
                        ).total_seconds()
                    )
                    > 6 * 3600
                ):
                    raise ValueError("Independent identity observation is outside six hours")
                distance = abs(
                    Geod(ellps="WGS84").inv(lon, lat, observation.longitude, observation.latitude)[
                        2
                    ]
                )
                if distance > observation.matching_radius_m:
                    raise ValueError("Observed object is outside the declared matching allowance")
                evidence.append(
                    Evidence(
                        kind=observation.kind,
                        reference=observation.reference,
                        acquired_utc=observation.acquired_utc,
                        rationale=observation.rationale,
                    )
                )
            common = {
                "scene_id": task["scene_id"],
                "reviewer_id": response.reviewer_id,
                "reviewed_utc": response.reviewed_utc,
                "acquired_utc": task["acquired_utc"],
                "longitude": lon,
                "latitude": lat,
                "evidence": evidence,
                "surveyed_area_id": task.get("base_task_id", task["task_id"]),
                "stage": task["stage"],
                "sampling_stratum": task["sampling_stratum"],
                "inclusion_probability": task["inclusion_probability"],
            }
            detection = Annotation(task="target_detection", label=mark.label, **common)
            identity = Annotation(task="target_identification", label=mark.identity, **common)
            normalized.append(
                {
                    "target_detection": detection.model_dump(mode="json"),
                    "target_identification": identity.model_dump(mode="json"),
                    "supporting_observations": [
                        o.model_dump(mode="json") for o in mark.supporting_observations
                    ],
                }
            )
            if mark.label == "uncertain":
                radius = workspace["policy"]["uncertain_buffer_pixels"]
                excluded[
                    max(0, mark.row - radius) : min(h, mark.row + radius + 1),
                    max(0, mark.col - radius) : min(w, mark.col + radius + 1),
                ] = True
        surveyed = domain & ~excluded if response.status == "complete" else np.zeros_like(domain)
        return {
            "response": response.model_dump(mode="json"),
            "base_task_id": task.get("base_task_id", task["task_id"]),
            "mode": task["mode"],
            "annotations": normalized,
            "source_sha256": task["source_sha256"],
            "scored_survey_pixels": int(surveyed.sum()) if task["mode"] == "survey" else 0,
            "uncertain_or_excluded_pixels": int(np.count_nonzero(domain & excluded)),
            "identity_evidence_is_analyst_attested": True,
            "authentication": "local import with explicit personal-inspection attestation; not an authenticated field-truth service",
        }


def ingest(root: Path, path: Path, now: datetime | None = None) -> dict[str, Any]:
    workspace = read_workspace(root)
    if path.stat().st_size > 20 * 1024**2:
        raise ValueError("Annotation import exceeds byte budget")
    imported = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(imported, list) or not imported:
        raise ValueError("Import requires a nonempty JSON array of explicit human responses")
    responses = [HumanResponse.model_validate(value) for value in imported]
    keys = [f"response:{r.phase}:{r.reviewer_id}:{r.task_id}" for r in responses]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate response in one import")
    current = now or datetime.now(UTC)
    with ledger(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        history = events(connection)
        staged = []
        for response in responses:
            tasks, issue = phase_tasks(root, history, response.phase)
            task = next((t for t in tasks if t["task_id"] == response.task_id), None)
            if task is None:
                raise ValueError("Unknown task; no detector candidate may be invented")
            if (
                response.phase == "initial"
                and task["mode"] == "candidate"
                and not completed_survey(history, tasks, response.reviewer_id)
            ):
                raise ValueError(
                    "Import the independent survey dispositions before candidate review"
                )
            staged.append(validate_response(root, workspace, response, task, issue, current))
        inserted = sum(
            append_event(connection, "response", key, body)
            for key, body in zip(keys, staged, strict=True)
        )
        total = sum(event["kind"] == "response" for event in events(connection))
    return {
        "inserted_responses": inserted,
        "total_responses": total,
        "import_sha256": file_digest(path),
        "operational_accuracy_claimed": False,
    }
