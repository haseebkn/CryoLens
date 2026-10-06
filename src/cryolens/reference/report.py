"""Measured review progress, sampling nonresponse, uncertainty and annotation effort."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from cryolens.eval.cohort import digest, file_digest
from cryolens.reference.consistency import agreement
from cryolens.reference.design import finite_population_interval
from cryolens.reference.packet import read_workspace
from cryolens.reference.records import events, ledger


def summarize(root: Path) -> dict[str, Any]:
    workspace = read_workspace(root)
    with ledger(root) as connection:
        history = events(connection)
    snapshots = []
    for event in (e for e in history if e["kind"] == "freeze"):
        receipt = event["body"]
        folder = (root / receipt["folder"]).resolve()
        if (
            not folder.is_relative_to(root.resolve())
            or file_digest(folder / "reference.json") != receipt["reference_sha256"]
        ):
            raise ValueError("Frozen reference snapshot changed")
        payload = receipt["payload"]
        for name, expected in payload["files_sha256"].items():
            path = (folder / name).resolve()
            if not path.is_relative_to(folder) or file_digest(path) != expected:
                raise ValueError("Frozen reference content changed")
        snapshots.append(
            {
                "folder": receipt["folder"],
                "payload_sha256": digest(payload),
                "surveyed_pixels": payload["surveyed_pixels"],
                "reference_set_complete": True,
            }
        )
    initial = [
        e["body"]
        for e in history
        if e["kind"] == "response" and e["body"]["response"]["phase"] == "initial"
    ]
    reviewers = sorted({b["response"]["reviewer_id"] for b in initial})
    reviewer_reports = []
    for reviewer in reviewers:
        bodies = [b for b in initial if b["response"]["reviewer_id"] == reviewer]
        survey = [b for b in bodies if b["mode"] == "survey"]
        candidates = [b for b in bodies if b["mode"] == "candidate"]
        effort = {}
        for mode, values, total in (
            ("survey", survey, workspace["survey_task_count"]),
            ("candidate", candidates, workspace["candidate_task_count"]),
        ):
            seconds = [b["response"]["active_seconds"] for b in values]
            effort[mode] = {
                "responded_tasks": len(values),
                "available_tasks": total,
                "measured_active_minutes": sum(seconds) / 60,
                "median_seconds_per_task": float(np.median(seconds)) if seconds else None,
                "mean_seconds_per_task": float(np.mean(seconds)) if seconds else None,
                "projected_remaining_minutes_using_observed_mean": (total - len(values))
                * float(np.mean(seconds))
                / 60
                if seconds
                else None,
                "projection_limitation": "Self-recorded review time and empirical task mix, not a confidence interval or regional labor estimate. Finish the whole-scene pilot before expanding.",
            }
        strata = []
        for design in workspace["sample_strata"]:
            responses = [
                b
                for b in candidates
                if b["annotations"]
                and b["annotations"][0]["target_detection"]["sampling_stratum"] == design["stratum"]
                and b["response"]["status"] == "complete"
            ]
            labels = [b["response"]["marks"][0]["label"] for b in responses]
            complete = len(responses) == design["selected_count"]
            weight = 1 / design["inclusion_probability"]
            interval = None
            if complete:
                lower = finite_population_interval(
                    design["population_count"], design["selected_count"], labels.count("target")
                )[0]
                upper = finite_population_interval(
                    design["population_count"],
                    design["selected_count"],
                    labels.count("target") + labels.count("uncertain"),
                )[1]
                interval = [lower, upper]
            strata.append(
                {
                    **design,
                    "complete_responses": len(responses),
                    "unresolved_or_nonresponse": design["selected_count"] - len(responses),
                    "target_labels": labels.count("target"),
                    "non_target_labels": labels.count("non_target"),
                    "uncertain_labels": labels.count("uncertain"),
                    "weighted_target_lower": weight * labels.count("target") if complete else None,
                    "weighted_target_upper_if_all_uncertain_are_targets": weight
                    * (labels.count("target") + labels.count("uncertain"))
                    if complete
                    else None,
                    "weighted_total_scope": "Raw generated component stratum only; no generation/masking recall or identity accuracy",
                    "regional_confidence_interval": None,
                    "conditional_95pct_population_target_count_envelope": interval,
                    "interval_limitation": "Exact per-stratum finite sampling uncertainty, expanding over uncertain labels. Not simultaneous across strata, not reviewer-error uncertainty or a regional scene/group interval.",
                }
            )
        labels = [a["target_detection"]["label"] for b in bodies for a in b["annotations"]]
        scored_pixels = sum(b["scored_survey_pixels"] for b in survey)
        reviewer_reports.append(
            {
                "reviewer_id": reviewer,
                "survey_dispositions": len(survey),
                "complete_survey_tasks": sum(b["response"]["status"] == "complete" for b in survey),
                "whole_scene_all_tasks_dispositioned": len(survey)
                == workspace["survey_task_count"],
                "whole_scene_all_tasks_complete": sum(
                    b["response"]["status"] == "complete" for b in survey
                )
                == workspace["survey_task_count"],
                "surveyed_unambiguous_pixels": scored_pixels,
                "approximate_surveyed_unambiguous_area_km2": scored_pixels
                * workspace["nominal_pixel_area_km2"],
                "unscored_available_pixels": workspace["survey_pixels"] - scored_pixels,
                "annotation_labels": {
                    label: labels.count(label) for label in ("target", "non_target", "uncertain")
                },
                "identity_unknown_count": sum(
                    a["target_identification"]["label"] == "unknown"
                    for b in bodies
                    for a in b["annotations"]
                ),
                "effort": effort,
                "candidate_sampling_results": strata,
            }
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace_id": workspace["workspace_id"],
        "scene_id": workspace["scene"]["scene_id"],
        "partition": workspace["scene"]["partition"],
        "survey_tasks_prepared": workspace["survey_task_count"],
        "candidate_tasks_prepared": workspace["candidate_task_count"],
        "raw_component_frame": workspace["detection"],
        "sampling_design": workspace["sample_strata"],
        "reference_only_test_access": workspace["reference_only_test_access"],
        "human_responses": sum(e["kind"] == "response" for e in history),
        "reviewer_count": len(reviewers),
        "reviewers": reviewer_reports,
        "consistency": agreement(history),
        "independent_reviewer_obtained": any(
            r["independent_review"] and r["paired_responses"] > 0 for r in agreement(history)
        ),
        "reference_set_complete": bool(snapshots),
        "frozen_reference_snapshots": snapshots,
        "completion_note": "Requires actual survey/candidate annotations, blinded consistency responses and an explicitly frozen adjudicated reference snapshot; software preparation alone does not complete this gate.",
        "target_detection_precision": None,
        "target_detection_recall": None,
        "empirical_false_alarms_per_1000km2": None,
        "iceberg_identification_accuracy": None,
        "claim_boundary": "SAR review may support observable-target detection metrics after independent survey and locked evaluation; it does not establish iceberg identity. No metrics are inferred from absent annotations.",
    }
