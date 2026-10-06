"""Prepare independent references, import human reviews and issue concealed re-reviews."""

from __future__ import annotations

import argparse
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryolens.reference.consistency import issue_review
from cryolens.reference.freeze import freeze_reference
from cryolens.reference.packet import prepare, read_workspace, write_json
from cryolens.reference.records import completed_survey, events, ingest, ledger, phase_tasks
from cryolens.reference.report import summarize


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("prepare")
    build.add_argument("--scene-id", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest", type=Path, default=Path("docs/evaluation/v1/manifest.json"))
    build.add_argument("--policy", type=Path, default=Path("configs/reference/protocol-v1.json"))
    build.add_argument("--data-root", type=Path, default=Path("data/raw"))
    build.add_argument("--survey-only", action="store_true")
    build.add_argument("--reference-release", type=Path)
    load = sub.add_parser("ingest")
    load.add_argument("--workspace", type=Path, required=True)
    load.add_argument("--input", type=Path, required=True)
    repeat = sub.add_parser("issue")
    repeat.add_argument("--workspace", type=Path, required=True)
    repeat.add_argument("--original-reviewer", required=True)
    repeat.add_argument("--reviewer", required=True)
    repeat.add_argument("--mode", choices=["repeat", "independent"], default="repeat")
    repeat.add_argument("--fraction", type=float)
    report = sub.add_parser("report")
    report.add_argument("--workspace", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("--workspace", type=Path, required=True)
    freeze.add_argument("--reviewer", required=True)
    freeze.add_argument("--adjudication", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    track = sub.add_parser("log")
    track.add_argument("--workspace", type=Path, required=True)
    track.add_argument("--tracking-uri", required=True)
    track.add_argument(
        "--artifact-root", type=Path, default=Path("data/processed/mlflow-artifacts")
    )
    serve = sub.add_parser("serve")
    serve.add_argument("--workspace", type=Path, required=True)
    serve.add_argument(
        "--queue", default="survey", help="survey, candidates, or an issued phase ID"
    )
    serve.add_argument(
        "--reviewer",
        help="Required for candidate/assigned queues; enter your ID in the survey form",
    )
    serve.add_argument("--port", type=int, default=8012)
    args = parser.parse_args()
    if args.command == "prepare":
        release = json.loads(args.reference_release.read_text()) if args.reference_release else None
        result = prepare(
            args.manifest,
            args.scene_id,
            args.data_root,
            args.output,
            args.policy,
            Path.cwd(),
            args.survey_only,
            release,
        )
        print(
            json.dumps(
                {
                    "workspace_id": result["workspace_id"],
                    "survey_tasks": result["survey_task_count"],
                    "candidate_tasks": result["candidate_task_count"],
                    "human_annotations": 0,
                }
            )
        )
    elif args.command == "ingest":
        print(json.dumps(ingest(args.workspace, args.input)))
    elif args.command == "issue":
        result = issue_review(
            args.workspace, args.original_reviewer, args.reviewer, args.mode, args.fraction
        )
        print(
            json.dumps(
                {
                    "phase": result["phase"],
                    "tasks": result["selected_count"],
                    "not_before_utc": result["not_before_utc"],
                    "folder": result["folder"],
                }
            )
        )
    elif args.command == "report":
        write_json(args.output, summarize(args.workspace))
        print(str(args.output))
    elif args.command == "freeze":
        result = freeze_reference(
            args.workspace, args.reviewer, json.loads(args.adjudication.read_text()), args.output
        )
        print(
            json.dumps(
                {
                    "reference_set_complete": result["reference_set_complete"],
                    "surveyed_pixels": result["surveyed_pixels"],
                }
            )
        )
    elif args.command == "log":
        from cryolens.reference.tracking import log_reference

        print(json.dumps(log_reference(args.workspace, args.tracking_uri, args.artifact_root)))
    else:
        read_workspace(args.workspace)
        if args.reviewer is not None and not args.reviewer.strip():
            raise ValueError("Reviewer ID cannot be blank")
        with ledger(args.workspace) as connection:
            history = events(connection)
            tasks, issue = phase_tasks(
                args.workspace,
                history,
                "initial" if args.queue in {"survey", "candidates"} else args.queue,
            )
            if args.queue == "candidates" and (
                args.reviewer is None or not completed_survey(history, tasks, args.reviewer)
            ):
                raise ValueError(
                    "Complete and import the independent survey dispositions before opening candidate review"
                )
            if issue is not None and issue["reviewer_id"] != args.reviewer:
                raise ValueError("Blinded packet is assigned to another reviewer")
        folder = (args.workspace / "review" / args.queue).resolve()
        if (
            not folder.is_relative_to((args.workspace / "review").resolve())
            or not (folder / "tasks.json").is_file()
        ):
            raise ValueError("Unknown review queue")
        handler = partial(SimpleHTTPRequestHandler, directory=str(folder))
        print(
            f"Human reference review: http://127.0.0.1:{args.port}/ ; only the reviewer folder is served",
            flush=True,
        )
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
