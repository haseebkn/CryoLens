"""Unit tests for PostGIS database models and repositories."""

from datetime import UTC, datetime

import pytest
import shapely.geometry
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cryolens.db.models import (
    Base,
)
from cryolens.db.repositories import DetectionRepository, SceneRepository


@pytest.fixture
def in_memory_db() -> sessionmaker[Session]:
    """Create an isolated SQLite database session factory for fast unit testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite fallback creates tables without spatial functions
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_create_and_query_scene(in_memory_db: sessionmaker[Session]) -> None:
    """Test creating and retrieving a SceneModel record."""
    with in_memory_db() as session:
        poly = shapely.geometry.box(-60.0, 43.5, -46.0, 55.0)
        scene = SceneRepository.create_scene(
            session=session,
            product_id="S1B_EW_GRDM_1SDH_20200515T094821_TEST",
            platform="Sentinel-1B",
            mode="EW",
            polarizations=["HH", "HV"],
            acquisition_time=datetime.now(UTC),
            cog_path="/data/processed/test.tif",
            footprint_epsg3978=poly,
            footprint_wgs84=poly,
            processing_provenance={"orbit": "POEORB", "engine": "python"},
            status="PROCESSED",
        )
        session.commit()

        assert scene.id is not None
        assert scene.product_id == "S1B_EW_GRDM_1SDH_20200515T094821_TEST"

        # Query by ID
        fetched = SceneRepository.get_by_id(session, scene.id)
        assert fetched is not None
        assert fetched.platform == "Sentinel-1B"
        assert fetched.polarizations == ["HH", "HV"]

        # Query by product_id
        by_prod = SceneRepository.get_by_product_id(
            session, "S1B_EW_GRDM_1SDH_20200515T094821_TEST"
        )
        assert by_prod is not None
        assert by_prod.id == scene.id


def test_create_and_validate_detection(in_memory_db: sessionmaker[Session]) -> None:
    """Test creating a DetectionModel and submitting a ValidationModel verdict."""
    with in_memory_db() as session:
        # Create scene first
        scene = SceneRepository.create_scene(
            session=session,
            product_id="S1A_EW_GRDM_1SDH_20200515T094821_DET_TEST",
            platform="Sentinel-1A",
            mode="EW",
            polarizations=["HH", "HV"],
            acquisition_time=datetime.now(UTC),
            cog_path="/data/processed/test_det.tif",
        )
        session.commit()

        # Create detection
        poly = shapely.geometry.box(-52.5, 47.2, -52.48, 47.22)
        pt = shapely.geometry.Point(-52.49, 47.21)

        det = DetectionRepository.create_detection(
            session=session,
            scene_id=scene.id,
            confidence=0.88,
            detector_name="CA-CFAR",
            predicted_class="iceberg",
            geom_wgs84=poly,
            centroid_wgs84=pt,
            length_m=75.0,
            width_m=45.0,
            estimated_area_m2=3375.0,
            peak_sigma0_hv_db=-15.2,
            mean_sigma0_hv_db=-18.4,
            peak_sigma0_hh_db=-11.0,
            hh_hv_ratio_db=4.2,
            incidence_angle_deg=34.5,
        )
        session.commit()

        assert det.id is not None
        assert det.length_m == 75.0
        assert det.predicted_class == "iceberg"

        # Record validation
        val = DetectionRepository.record_validation(
            session=session,
            detection_id=det.id,
            analyst_verdict="CONFIRMED_ICEBERG",
            analyst_id="analyst_qa",
            notes="Clearly identifiable iceberg signature with volume scattering shadow.",
        )
        session.commit()

        assert val.id is not None
        assert val.analyst_verdict == "CONFIRMED_ICEBERG"
        assert len(det.validations) == 1
