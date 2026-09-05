"""API behavior using SQLite geometry predicates, plus separate real PostGIS CI checks."""

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
import shapely.geometry
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cryolens.api.main import app
from cryolens.config.settings import get_settings
from cryolens.db.models import Base, DetectionModel, SceneModel, ValidationModel
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session
from tests.spatial_sqlite import register_spatial_functions

KEY = "test-only-review-key-not-a-production-credential"


@pytest.fixture
def api_database() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    register_spatial_functions(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as session:
        polygon = shapely.geometry.box(-53, 47, -52, 48)
        scene = SceneRepository.create_scene(
            session,
            product_id="S1B_EW_GRDM_1SDH_API_TEST",
            platform="Sentinel-1B",
            mode="EW",
            polarizations=["HH", "HV"],
            acquisition_time=datetime(2020, 5, 15, 10, tzinfo=UTC),
            cog_path="/data/private/api_test.tif",
            footprint_wgs84=polygon,
        )
        DetectionRepository.create_detection(
            session,
            scene_id=scene.id,
            confidence=0.92,
            detector_name="K-CFAR",
            predicted_class="iceberg",
            geom_wgs84=polygon,
            centroid_wgs84=shapely.geometry.Point(-52.5, 47.5),
            length_m=80,
            width_m=40,
            peak_sigma0_hv_db=-14.8,
        )
        session.commit()
    yield factory
    engine.dispose()


@pytest.fixture
def api_test_client(
    api_database: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    monkeypatch.setenv("CRYOLENS_ANALYST_API_KEY", KEY)
    monkeypatch.setenv("CRYOLENS_ANALYST_ID", "portfolio_reviewer")
    get_settings.cache_clear()

    def override_session() -> Generator[Session, None, None]:
        with api_database() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db_session] = override_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        get_settings.cache_clear()


def first_id(client: TestClient) -> str:
    return str(client.get("/api/v1/detections").json()["features"][0]["id"])


def test_health_does_not_leak_database_errors(api_test_client: TestClient) -> None:
    response = api_test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"  # SQLite has no PostGIS extension.
    assert response.json()["database"] == "unavailable"
    assert "SELECT" not in response.text and "OperationalError" not in response.text


def test_list_scenes_geojson(api_test_client: TestClient) -> None:
    response = api_test_client.get("/api/v1/scenes")
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "FeatureCollection"
    assert data["features"][0]["properties"]["product_id"] == "S1B_EW_GRDM_1SDH_API_TEST"
    assert "cog_path" not in data["features"][0]["properties"]


def test_raw_iceberg_class_is_not_confirmation(api_test_client: TestClient) -> None:
    properties = api_test_client.get("/api/v1/detections").json()["features"][0]["properties"]
    assert properties["predicted_class"] == "iceberg"
    assert properties["display_class"] == "unclassified"
    assert properties["assessment_status"] == "unverified_candidate"
    assert properties["confidence"] == 0.92
    assert properties["confidence_kind"] == "uncalibrated_detector_score"
    assert properties["ais_status"] == "unavailable"
    assert properties["peak_sigma0_hv_db"] == -14.8
    assert properties["centroid"] == [-52.5, 47.5]


def test_authenticated_review_preserves_raw_class(api_test_client: TestClient) -> None:
    detection_id = first_id(api_test_client)
    response = api_test_client.post(
        f"/api/v1/detections/{detection_id}/validate",
        headers={"X-Analyst-Key": KEY},
        json={
            "analyst_verdict": "VESSEL",
            "notes": "Matched to an independently sourced vessel observation.",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["analyst_id"] == "portfolio_reviewer"
    assert response.json()["corrected_class"] == "ship"
    properties = api_test_client.get(f"/api/v1/detections/{detection_id}").json()["properties"]
    assert properties["validated"] is True
    assert properties["display_class"] == "ship"
    assert properties["predicted_class"] == "iceberg"


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_review_requires_valid_key(api_test_client: TestClient, key: str | None) -> None:
    response = api_test_client.post(
        f"/api/v1/detections/{first_id(api_test_client)}/validate",
        headers={"X-Analyst-Key": key} if key else {},
        json={"analyst_verdict": "VESSEL", "notes": "Independent corroborating observation."},
    )
    assert response.status_code == 401


def test_review_disabled_without_configured_identity(
    api_test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRYOLENS_ANALYST_ID", "")
    get_settings.cache_clear()
    response = api_test_client.post(
        f"/api/v1/detections/{first_id(api_test_client)}/validate",
        headers={"X-Analyst-Key": KEY},
        json={"analyst_verdict": "VESSEL", "notes": "Independent corroborating observation."},
    )
    assert response.status_code == 503
    assert api_test_client.get("/api/v1/capabilities").json()["analyst_writes_enabled"] is False


@pytest.mark.parametrize(
    "payload,status",
    [
        (
            {
                "analyst_verdict": "VESSEL",
                "notes": "Independent observation.",
                "analyst_id": "impersonated_person",
            },
            403,
        ),
        (
            {
                "analyst_verdict": "VESSEL",
                "notes": "Independent observation.",
                "corrected_class": "iceberg",
            },
            422,
        ),
        ({"analyst_verdict": "VESSEL", "notes": "         "}, 422),
        ({"analyst_verdict": "VESSEL"}, 422),
    ],
)
def test_review_rejects_impersonation_and_contradictory_evidence(
    api_test_client: TestClient, payload: dict[str, str], status: int
) -> None:
    response = api_test_client.post(
        f"/api/v1/detections/{first_id(api_test_client)}/validate",
        headers={"X-Analyst-Key": KEY},
        json=payload,
    )
    assert response.status_code == status


@pytest.mark.parametrize(
    "bbox",
    [
        "1,2,3",
        "nan,45,-50,50",
        "-60,45,inf,50",
        "-50,45,-60,50",
        "-60,55,-50,45",
        "-181,45,-50,50",
        "-60,45,-50,91",
        "",
    ],
)
@pytest.mark.parametrize("path", ["detections", "iip"])
def test_invalid_spatial_bounds(api_test_client: TestClient, bbox: str, path: str) -> None:
    assert api_test_client.get(f"/api/v1/{path}", params={"bbox": bbox}).status_code == 400


@pytest.mark.parametrize("path", ["detections", "scenes", "iip"])
def test_invalid_temporal_bounds(api_test_client: TestClient, path: str) -> None:
    assert (
        api_test_client.get(
            f"/api/v1/{path}", params={"start_date": "2020-05-15T10:00:00"}
        ).status_code
        == 400
    )
    assert (
        api_test_client.get(
            f"/api/v1/{path}",
            params={"start_date": "2021-01-01T00:00:00Z", "end_date": "2020-01-01T00:00:00Z"},
        ).status_code
        == 400
    )


def test_iip_pagination_is_bounded(api_test_client: TestClient) -> None:
    assert api_test_client.get("/api/v1/iip?limit=-1").status_code == 422
    assert api_test_client.get("/api/v1/iip?offset=-1").status_code == 422


def test_aoi_filter_precedes_pagination(
    api_test_client: TestClient, api_database: sessionmaker[Session]
) -> None:
    inside_id = first_id(api_test_client)
    with api_database() as session:
        scene = session.query(SceneModel).first()
        assert scene is not None
        outside = DetectionRepository.create_detection(
            session,
            scene_id=scene.id,
            confidence=0.99,
            detector_name="TEST",
            centroid_wgs84=shapely.geometry.Point(-64, 45),
        )
        outside.created_at = datetime.now(UTC) + timedelta(days=1)
        session.commit()
        outside_id = outside.id
    response = api_test_client.get("/api/v1/detections?limit=1")
    assert response.status_code == 200
    assert response.json()["features"][0]["id"] == inside_id
    assert api_test_client.get(f"/api/v1/detections/{outside_id}").status_code == 404
    assert (
        api_test_client.get("/api/v1/detections", params={"bbox": "-65,44,-63,46"}).json()[
            "features"
        ]
        == []
    )


def test_unknown_score_is_not_fabricated(
    api_test_client: TestClient, api_database: sessionmaker[Session]
) -> None:
    detection_id = first_id(api_test_client)
    with api_database() as session:
        detection = session.get(DetectionModel, detection_id)
        assert detection is not None
        detection.confidence = None
        session.commit()
    assert (
        api_test_client.get(f"/api/v1/detections/{detection_id}").json()["properties"]["confidence"]
        is None
    )
    assert api_test_client.get("/api/v1/detections?min_confidence=0.5").json()["features"] == []


def test_latest_review_selected_by_time(
    api_test_client: TestClient, api_database: sessionmaker[Session]
) -> None:
    detection_id = first_id(api_test_client)
    with api_database() as session:
        # Insert newest first; relationship insertion order must not determine verdict.
        session.add_all(
            [
                ValidationModel(
                    detection_id=detection_id,
                    analyst_verdict="VESSEL",
                    validated_at=datetime(2020, 2, 1, tzinfo=UTC),
                ),
                ValidationModel(
                    detection_id=detection_id,
                    analyst_verdict="CONFIRMED_ICEBERG",
                    validated_at=datetime(2020, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    properties = api_test_client.get(f"/api/v1/detections/{detection_id}").json()["properties"]
    assert properties["analyst_verdict"] == "VESSEL"
    assert properties["display_class"] == "ship"


def test_synthetic_records_are_not_presented_as_observations(
    api_test_client: TestClient, api_database: sessionmaker[Session]
) -> None:
    detection_id = first_id(api_test_client)
    with api_database() as session:
        scene = session.query(SceneModel).first()
        assert scene is not None
        scene.processing_provenance = {"synthetic": True}
        session.commit()
        scene_id = scene.id
    assert api_test_client.get("/api/v1/scenes").json()["features"] == []
    assert api_test_client.get("/api/v1/detections").json()["features"] == []
    assert api_test_client.get(f"/api/v1/scenes/{scene_id}").status_code == 404
    assert api_test_client.get(f"/api/v1/detections/{detection_id}").status_code == 404


def test_unsupported_drift_and_honest_capabilities(api_test_client: TestClient) -> None:
    assert api_test_client.get("/api/v1/drift/anything").status_code == 501
    data = api_test_client.get("/api/v1/capabilities").json()
    assert data["operational_use"] is False and data["ais"] == "unavailable"
    assert data["aoi"]["geometry"]["type"] in {"Polygon", "MultiPolygon"}


def test_dashboard_uses_honest_labels(api_test_client: TestClient) -> None:
    response = api_test_client.get("/")
    assert response.status_code == 200
    assert "Unverified SAR candidates" in response.text
    assert "No C-CORE affiliation" in response.text
    assert "Analyst-confirmed icebergs only" in response.text
