"""Read verified generated pairs and append attributable optical evidence."""

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Geod
from sqlalchemy import select
from sqlalchemy.orm import Session

from cryolens.api.routes.detections import _in_scope
from cryolens.api.security import require_analyst
from cryolens.config.settings import get_settings
from cryolens.db.models import DetectionModel, OpticalEvidenceModel, OpticalPairModel
from cryolens.db.session import get_db_session
from cryolens.eval.cohort import file_digest

router = APIRouter(prefix="/detections", tags=["Optical evidence"])


class OpticalEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    assessment: Literal["corroborating", "ambiguous", "unavailable", "no_visible_counterpart"]
    visibility: Literal["clear", "obscured", "uncertain", "unavailable"]
    observed_lonlat: tuple[float, float] | None = None
    notes: str = Field(min_length=10, max_length=4000)

    @model_validator(mode="after")
    def validate_evidence(self) -> "OpticalEvidenceRequest":
        if self.observed_lonlat is not None:
            lon, lat = self.observed_lonlat
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError("Observed optical position must be valid WGS84")
        if self.assessment == "corroborating" and (
            self.visibility != "clear" or self.observed_lonlat is None
        ):
            raise ValueError(
                "Corroboration requires inspected clear visibility and an optical position"
            )
        if self.assessment == "no_visible_counterpart" and self.visibility != "clear":
            raise ValueError("No-visible-counterpart evidence requires inspected clear visibility")
        return self


def _target(session: Session, detection_id: str) -> DetectionModel:
    target = session.get(DetectionModel, detection_id)
    if target is None or not _in_scope(target):
        raise HTTPException(404, "Detection not found in the NL study area")
    return target


def _pair(session: Session, detection_id: str, pair_id: str) -> OpticalPairModel:
    _target(session, detection_id)
    pair = session.get(OpticalPairModel, pair_id)
    if pair is None or pair.detection_id != detection_id:
        raise HTTPException(404, "Optical pair not found for this candidate")
    return pair


def _artifact(pair: OpticalPairModel, filename: str) -> Path:
    root = (get_settings().data_dir / "processed/optical-review").resolve()
    folder = (root / pair.id).resolve()
    if not folder.is_relative_to(root) or Path(filename).name != filename:
        raise HTTPException(400, "Invalid artifact path")
    manifest_path = folder / "pair.json"
    if not manifest_path.is_file() or file_digest(manifest_path) != pair.manifest_sha256:
        raise HTTPException(
            409, "Pair manifest is unavailable or changed; regenerate and review provenance"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if filename not in manifest["files"]:
        raise HTTPException(404, "Artifact not recorded in this pair")
    path = folder / filename
    if (
        not path.resolve().is_relative_to(folder)
        or not path.is_file()
        or file_digest(path) != manifest["files"][filename]
    ):
        raise HTTPException(409, "Pair artifact is unavailable or changed")
    return path


def _history(session: Session, pair_id: str) -> list[dict[str, Any]]:
    records = session.scalars(
        select(OpticalEvidenceModel)
        .where(OpticalEvidenceModel.pair_id == pair_id)
        .order_by(OpticalEvidenceModel.created_at, OpticalEvidenceModel.id)
    )
    return [
        {
            "id": r.id,
            "assessment": r.assessment,
            "visibility": r.visibility,
            "observed_lonlat": r.observed_lonlat,
            "notes": r.notes,
            "analyst_id": r.analyst_id,
            "created_at": r.created_at.isoformat(),
        }
        for r in records
    ]


@router.get("/{detection_id}/optical-pairs")
def list_pairs(detection_id: str, session: Session = Depends(get_db_session)) -> dict[str, Any]:
    _target(session, detection_id)
    pairs = session.scalars(
        select(OpticalPairModel)
        .where(OpticalPairModel.detection_id == detection_id)
        .order_by(OpticalPairModel.created_at.desc(), OpticalPairModel.id)
        .limit(100)
    )
    return {
        "pairs": [
            {
                **p.metadata_json,
                "evidence_history": _history(session, p.id),
                "image_url": f"/api/v1/detections/{detection_id}/optical-pairs/{p.id}/assets/paired.png",
            }
            for p in pairs
        ],
        "missing_optical_is_false_target": False,
    }


@router.get("/{detection_id}/optical-pairs/{pair_id}/assets/{filename}")
def pair_asset(
    detection_id: str, pair_id: str, filename: str, session: Session = Depends(get_db_session)
) -> FileResponse:
    pair = _pair(session, detection_id, pair_id)
    path = _artifact(pair, filename)
    return FileResponse(
        path,
        media_type="image/png"
        if filename.endswith(".png")
        else "image/tiff"
        if filename.endswith(".tif")
        else "application/json",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/{detection_id}/optical-pairs/{pair_id}/evidence", status_code=201)
def record_evidence(
    detection_id: str,
    pair_id: str,
    body: OpticalEvidenceRequest,
    analyst: str = Depends(require_analyst),
    session: Session = Depends(get_db_session),
) -> dict[str, Any]:
    pair = _pair(session, detection_id, pair_id)
    _artifact(pair, "paired.png")
    metadata = pair.metadata_json
    if body.assessment == "no_visible_counterpart" and not metadata.get("optical_item_id"):
        raise HTTPException(
            422, "No optical acquisition is unavailable evidence, not an absent counterpart"
        )
    if body.assessment == "no_visible_counterpart" and (
        not metadata.get("full_movement_envelope_in_chip")
        or not metadata.get("visibility", {}).get("useful_for_review")
    ):
        raise HTTPException(
            422,
            "Incomplete displacement coverage or limited visibility cannot support an absence observation; record ambiguity or unavailability",
        )
    if body.assessment == "corroborating":
        if not metadata.get("optical_item_id"):
            raise HTTPException(422, "Unavailable optical imagery cannot corroborate a candidate")
        lon, lat = body.observed_lonlat or (0, 0)
        center = metadata["candidate_lonlat"]
        _, _, distance = Geod(ellps="WGS84").inv(center[0], center[1], lon, lat)
        if distance > metadata["matching_radius_m"]:
            raise HTTPException(
                422, "Optical position exceeds the recorded movement/geolocation envelope"
            )
        from pyproj import Transformer

        x, y = Transformer.from_crs(4326, 3978, always_xy=True).transform(lon, lat)
        transform = metadata["alignment"]["transform"]
        row = (y - transform[5]) / transform[4]
        col = (x - transform[2]) / transform[0]
        height, width = metadata["alignment"]["shape"]
        if not (0 <= row < height and 0 <= col < width):
            raise HTTPException(422, "Optical position is outside the inspected chip")
        import numpy as np
        import rasterio
        from rasterio.windows import Window

        with rasterio.open(_artifact(pair, "optical-aligned.tif")) as raster:
            window = Window(int(col), int(row), 1, 1)
            values = raster.read(window=window)[:, 0, 0]
            valid = bool(raster.dataset_mask(window=window)[0, 0])
        if (
            not valid
            or not np.isfinite(values).all()
            or np.any(values[:3] <= 0)
            or values[-1] in (0, 1)
        ):
            raise HTTPException(422, "Optical position has no valid observed pixels")
    evidence = OpticalEvidenceModel(
        pair_id=pair.id,
        assessment=body.assessment,
        visibility=body.visibility,
        observed_lonlat=list(body.observed_lonlat) if body.observed_lonlat else None,
        analyst_id=analyst,
        notes=body.notes,
    )
    session.add(evidence)
    session.flush()
    return {
        "id": evidence.id,
        "assessment": evidence.assessment,
        "analyst_id": analyst,
        "radar_verdict_changed": False,
    }
