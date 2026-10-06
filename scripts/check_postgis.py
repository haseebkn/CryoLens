"""Exercise real PostGIS without the unit suite's SQLite geometry substitutes.

Run after `alembic upgrade head`. All fixture data is rolled back, including
when a check fails; this script never deletes or replaces existing records.
"""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from geoalchemy2.shape import to_shape
from shapely.geometry import Point, box
from sqlalchemy import text
from sqlalchemy.orm import Session

from cryolens.api.main import app
from cryolens.db.models import OpticalEvidenceModel, OpticalPairModel
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session, get_db_session_factory


def main() -> None:
    with get_db_session_factory()() as session:
        try:
            assert session.scalar(text("SELECT PostGIS_Version()"))
            suffix = uuid.uuid4().hex
            scene = SceneRepository.create_scene(
                session,
                product_id="TEST_ROLLBACK_" + suffix,
                platform="TEST",
                mode="EW",
                polarizations=["HH", "HV"],
                acquisition_time=datetime(2020, 5, 15, tzinfo=UTC),
                cog_path="test-fixture-not-a-raster",
                footprint_wgs84=box(-53, 47, -51, 49),
                processing_provenance={"purpose": "transaction rollback test"},
            )
            inside = DetectionRepository.create_detection(
                session,
                scene_id=scene.id,
                confidence=0.4,
                detector_name="TEST",
                predicted_class="unclassified",
                centroid_wgs84=Point(-52, 48),
                geom_wgs84=box(-52.01, 47.99, -51.99, 48.01),
            )
            outside = DetectionRepository.create_detection(
                session,
                scene_id=scene.id,
                confidence=0.8,
                detector_name="TEST",
                predicted_class="unclassified",
                centroid_wgs84=Point(-64, 45),
                geom_wgs84=box(-64.01, 44.99, -63.99, 45.01),
            )
            session.expire_all()
            assert to_shape(inside.centroid_wgs84).equals(Point(-52, 48))
            pair = OpticalPairModel(
                id=uuid.uuid4().hex,
                detection_id=inside.id,
                manifest_sha256="0" * 64,
                metadata_json={"purpose": "rollback-only PostGIS fixture", "optical_item_id": None},
            )
            session.add(pair)
            session.flush()
            evidence = OpticalEvidenceModel(
                pair_id=pair.id,
                assessment="unavailable",
                visibility="unavailable",
                analyst_id="TEST_ROLLBACK",
                notes="Rollback-only persistence check; no actual optical judgment.",
            )
            session.add(evidence)
            session.flush()
            session.expire_all()
            assert session.get(OpticalPairModel, pair.id).metadata_json["optical_item_id"] is None
            assert session.get(OpticalEvidenceModel, evidence.id).assessment == "unavailable"
            assert inside.predicted_class == "unclassified"

            def override_session() -> Generator[Session, None, None]:
                yield session

            app.dependency_overrides[get_db_session] = override_session
            with TestClient(app) as client:
                response = client.get("/api/v1/detections", params={"scene_id": scene.id})
                assert response.status_code == 200, response.text
                ids = {f["id"] for f in response.json()["features"]}
                assert inside.id in ids, response.text
                assert outside.id not in ids, response.text
                bad = client.get("/api/v1/detections", params={"bbox": "-60,55,-65,50"})
                assert bad.status_code == 400, bad.text
                optical = client.get(f"/api/v1/detections/{inside.id}/optical-pairs")
                assert optical.status_code == 200, optical.text
                assert (
                    optical.json()["pairs"][0]["evidence_history"][0]["assessment"] == "unavailable"
                )
            print(
                "Real PostGIS geometry, NL restriction and optical evidence persistence passed; fixtures rolled back."
            )
        finally:
            app.dependency_overrides.clear()
            session.rollback()


if __name__ == "__main__":
    main()
