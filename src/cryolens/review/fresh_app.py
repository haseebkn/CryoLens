"""Local authenticated analyst interface for qualified SAFE review locations."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field, model_validator
from pyproj import Geod, Transformer
from rasterio.windows import Window

from cryolens.api.routes.optical import OpticalEvidenceRequest
from cryolens.api.security import require_analyst
from cryolens.eval.cohort import file_digest
from cryolens.geo.aoi import contains_point
from cryolens.reference.records import append_event, events, ledger
from cryolens.review.pairs import verify_pair_files


class FreshEvidence(OpticalEvidenceRequest):
    personally_inspected: Literal[True]
    sar_observed_lonlat: tuple[float, float] | None = None
    viewed_channels: list[Literal["HH", "HV"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def observed_sar(self) -> FreshEvidence:
        if self.sar_observed_lonlat is not None:
            lon, lat = self.sar_observed_lonlat
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError("SAR position must be finite WGS84")
        if self.assessment in {"corroborating", "no_visible_counterpart"} and (
            self.sar_observed_lonlat is None or set(self.viewed_channels) != {"HH", "HV"}
        ):
            raise ValueError(
                "A sampled location is not a target: identify an inspected SAR position in both channels first"
            )
        return self


def valid_point(
    path: Path, position: tuple[float, float], bands: list[int], cloud_buffer_m: float = 0
) -> None:
    if not contains_point(*position):
        raise ValueError("Observation must lie in the NL study polygon")
    with rasterio.open(path) as src:
        x, y = Transformer.from_crs(4326, src.crs, always_xy=True).transform(*position)
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            raise ValueError("Observation lies outside inspected imagery")
        data = src.read(bands, window=Window(col, row, 1, 1), masked=True)
        if np.ma.getmaskarray(data).any() or not np.isfinite(data.data).all():
            raise ValueError("Observation has no valid image pixels")
        if len(bands) == 5 and (
            np.any(data.data[:3] <= 0) or data.data[-1, 0, 0] not in (4, 5, 6, 11)
        ):
            raise ValueError("Optical position is invalid, obscured or SCL-ambiguous")
        if len(bands) == 5 and cloud_buffer_m:
            radius = int(np.ceil(cloud_buffer_m / abs(src.transform.a)))
            left, top = max(0, col - radius), max(0, row - radius)
            width, height = (
                min(src.width, col + radius + 1) - left,
                min(src.height, row + radius + 1) - top,
            )
            scl = src.read(5, window=Window(left, top, width, height))
            yy, xx = np.ogrid[top : top + height, left : left + width]
            disk = (xx - col) ** 2 + (yy - row) ** 2 <= radius**2
            if np.any(np.isin(scl, [3, 8, 9, 10]) & disk):
                raise ValueError("Optical position is inside the frozen cloud/shadow buffer")


def validate_evidence(folder: Path, pair: dict[str, Any], body: FreshEvidence) -> None:
    if body.sar_observed_lonlat is not None:
        valid_point(folder / "radar-native.tif", body.sar_observed_lonlat, [1, 2])
    if (
        body.assessment in {"corroborating", "no_visible_counterpart"}
        and not pair["optical_item_id"]
    ):
        raise ValueError("Missing optical imagery supports unavailability, not presence or absence")
    if body.assessment == "corroborating":
        assert body.sar_observed_lonlat is not None and body.observed_lonlat is not None
        valid_point(
            folder / "optical-aligned.tif",
            body.observed_lonlat,
            [1, 2, 3, 4, 5],
            pair["policy"]["cloud_buffer_m"],
        )
        distance = Geod(ellps="WGS84").inv(*body.sar_observed_lonlat, *body.observed_lonlat)[2]
        if distance > pair["matching_radius_m"]:
            raise ValueError(
                "Observed SAR/optical positions exceed the movement/geolocation allowance"
            )
    if body.assessment == "no_visible_counterpart":
        assert body.sar_observed_lonlat is not None
        x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(*body.sar_observed_lonlat)
        with rasterio.open(folder / "optical-aligned.tif") as src:
            b = src.bounds
            full = (
                min(x - b.left, b.right - x, y - b.bottom, b.top - y) >= pair["matching_radius_m"]
            )
        # Screening was computed about the sampled centre. A displaced SAR point needs a new pair.
        distance = Geod(ellps="WGS84").inv(
            pair["candidate_lonlat"][0],
            pair["candidate_lonlat"][1],
            body.sar_observed_lonlat[0],
            body.sar_observed_lonlat[1],
        )[2]
        if distance > 1 or not full or not pair["visibility"]["useful_for_review"]:
            raise ValueError(
                "Absence needs full screened coverage about the SAR point; create a centred pair or record ambiguity"
            )


def create_app(root: Path) -> FastAPI:
    root = root.resolve()
    report_path = root / "report.json"
    checksum = file_digest(report_path)
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    pairs = {p["id"]: p for r in report["locations"] for p in r["pairs"]}
    evidence_root = root / "evidence"
    evidence_root.mkdir(exist_ok=True)
    with ledger(evidence_root):
        pass
    app = FastAPI(title="CryoLens SAFE optical review")

    def verified(identifier: str) -> tuple[Path, dict[str, Any]]:
        if identifier not in pairs:
            raise HTTPException(404, "Unknown pair")
        folder = root / "pairs" / identifier
        path = folder / "pair.json"
        if (
            file_digest(report_path) != checksum
            or not path.is_file()
            or file_digest(path) != pairs[identifier]["manifest_sha256"]
        ):
            raise HTTPException(409, "Review provenance changed")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["id"] != identifier or manifest.get("subject_kind") != "review_location":
            raise HTTPException(409, "Review location identity changed")
        try:
            verify_pair_files(folder, manifest)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return folder, manifest

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(Path(__file__).parents[1] / "web/static/fresh-optical.html")

    @app.get("/api/report")
    def get_report() -> dict[str, Any]:
        if file_digest(report_path) != checksum:
            raise HTTPException(409, "Review report changed")
        return report

    @app.get("/api/pairs/{identifier}")
    def get_pair(identifier: str) -> dict[str, Any]:
        _, manifest = verified(identifier)
        with ledger(evidence_root) as connection:
            history = [e["body"] for e in events(connection) if e["body"]["pair_id"] == identifier]
        return {**manifest, "evidence_history": history}

    @app.get("/api/pairs/{identifier}/assets/{filename}")
    def asset(identifier: str, filename: str) -> FileResponse:
        folder, manifest = verified(identifier)
        if filename not in manifest["files"] or Path(filename).name != filename:
            raise HTTPException(404, "Unrecorded asset")
        return FileResponse(folder / filename, headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/api/pairs/{identifier}/evidence", status_code=201)
    def evidence(
        identifier: str, body: FreshEvidence, analyst: str = Depends(require_analyst)
    ) -> dict[str, Any]:
        folder, manifest = verified(identifier)
        try:
            validate_evidence(folder, manifest, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        record = {
            "id": uuid.uuid4().hex,
            "pair_id": identifier,
            "pair_manifest_sha256": pairs[identifier]["manifest_sha256"],
            "analyst_id": analyst,
            "recorded_at": datetime.now(UTC).isoformat(),
            "observation": body.model_dump(mode="json"),
            "radar_verdict_changed": False,
            "identity": "unknown",
            "claim_boundary": "Analyst optical evidence only; not independent ship/iceberg truth or a model accuracy measurement",
        }
        with ledger(evidence_root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            append_event(connection, "optical_evidence", record["id"], record)
        return record

    return app
