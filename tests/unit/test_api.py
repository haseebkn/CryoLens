"""Unit tests for FastAPI REST endpoints and GeoJSON outputs."""

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
import shapely.geometry
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cryolens.api.main import app
from cryolens.db.models import Base
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session


@pytest.fixture
def api_test_client() -> TestClient:
    """Fixture providing a TestClient with an isolated in-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db_session() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    client = TestClient(app)

    # Seed test data
    with session_factory() as session:
        poly = shapely.geometry.box(-53.0, 47.0, -52.0, 48.0)
        scene = SceneRepository.create_scene(
            session=session,
            product_id="S1B_EW_GRDM_1SDH_API_TEST",
            platform="Sentinel-1B",
            mode="EW",
            polarizations=["HH", "HV"],
            acquisition_time=datetime.now(UTC),
            cog_path="/data/processed/api_test.tif",
            footprint_wgs84=poly,
            status="DETECTED",
        )
        session.commit()

        # Seed detection
        DetectionRepository.create_detection(
            session=session,
            scene_id=scene.id,
            confidence=0.92,
            detector_name="K-CFAR",
            predicted_class="iceberg",
            geom_wgs84=poly,
            centroid_wgs84=shapely.geometry.Point(-52.5, 47.5),
            length_m=80.0,
            width_m=40.0,
            estimated_area_m2=3200.0,
            peak_sigma0_hv_db=-14.8,
            mean_sigma0_hv_db=-17.2,
            peak_sigma0_hh_db=-10.0,
            hh_hv_ratio_db=4.8,
            incidence_angle_deg=35.0,
        )
        session.commit()

    return client


def test_health_endpoint(api_test_client: TestClient) -> None:
    """Test /health endpoint status."""
    res = api_test_client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] in ("healthy", "degraded")
    assert "database" in data
    assert "version" in data


def test_list_scenes_geojson(api_test_client: TestClient) -> None:
    """Test /api/v1/scenes returns standard GeoJSON FeatureCollection."""
    res = api_test_client.get("/api/v1/scenes")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) >= 1

    feature = data["features"][0]
    assert feature["type"] == "Feature"
    assert "geometry" in feature
    assert feature["properties"]["product_id"] == "S1B_EW_GRDM_1SDH_API_TEST"


def test_list_detections_geojson(api_test_client: TestClient) -> None:
    """Test /api/v1/detections returns standard GeoJSON FeatureCollection."""
    res = api_test_client.get("/api/v1/detections")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) >= 1

    feature = data["features"][0]
    assert feature["type"] == "Feature"
    p = feature["properties"]
    assert p["predicted_class"] == "iceberg"
    assert p["confidence"] == 0.92
    assert p["peak_sigma0_hv_db"] == -14.8
    assert p["length_m"] == 80.0


def test_validate_detection(api_test_client: TestClient) -> None:
    """Test submitting analyst validation via POST /api/v1/detections/{id}/validate."""
    # Get detection ID
    list_res = api_test_client.get("/api/v1/detections")
    det_id = list_res.json()["features"][0]["properties"]["id"]

    # Submit validation
    payload = {
        "analyst_verdict": "CONFIRMED_ICEBERG",
        "corrected_class": "iceberg",
        "analyst_id": "c-core_operator",
        "notes": "Verified against high target contrast.",
    }
    val_res = api_test_client.post(f"/api/v1/detections/{det_id}/validate", json=payload)
    assert val_res.status_code == 201
    val_data = val_res.json()
    assert val_data["detection_id"] == det_id
    assert val_data["analyst_verdict"] == "CONFIRMED_ICEBERG"

    # Query detection again to verify validation state is reflected
    get_res = api_test_client.get(f"/api/v1/detections/{det_id}")
    assert get_res.status_code == 200
    assert get_res.json()["properties"]["validated"] is True
    assert get_res.json()["properties"]["analyst_verdict"] == "CONFIRMED_ICEBERG"


def test_serve_dashboard_index(api_test_client: TestClient) -> None:
    """Test root / serves the HTML Leaflet dashboard."""
    res = api_test_client.get("/")
    assert res.status_code == 200
    assert "CryoLens" in res.text
    assert "leaflet" in res.text.lower()
