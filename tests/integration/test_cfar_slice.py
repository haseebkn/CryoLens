"""End-to-end integration test for Phase 2 thin vertical slice.

Pipeline sequence:
Synthetic 4-Band COG in EPSG:3978 -> CFAR Detection -> Vectorization -> PostGIS -> FastAPI GeoJSON
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
import rasterio.transform
import shapely.geometry
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from cryolens.api.main import app
from cryolens.db.models import Base
from cryolens.db.repositories import DetectionRepository, SceneRepository
from cryolens.db.session import get_db_session
from cryolens.detect.cfar import GammaCFARDetector
from cryolens.geo.vectorize import TargetVectorizer
from cryolens.preprocess.stack import COGStackBuilder


@pytest.fixture
def synthetic_4band_cog(tmp_path: Path) -> Path:
    """Generate a calibrated 4-band synthetic COG in EPSG:3978 with injected iceberg targets."""
    h, w = 120, 120
    np.random.seed(42)

    # 1. Background ocean: mean HV = -32 dB, mean HH = -22 dB
    mean_linear_hv = 10.0 ** (-32.0 / 10.0)
    mean_linear_hh = 10.0 ** (-22.0 / 10.0)

    hh_db = 10.0 * np.log10(
        np.maximum(np.random.exponential(scale=mean_linear_hh, size=(h, w)), 1e-10)
    )
    hv_db = 10.0 * np.log10(
        np.maximum(np.random.exponential(scale=mean_linear_hv, size=(h, w)), 1e-10)
    )
    inc_deg = np.full((h, w), 35.0, dtype=np.float32)

    # 2. Inject 3 Icebergs (High cross-pol HV backscatter)
    # Target 1: Medium Iceberg at (35:38, 35:38) -> 3x3 px
    hv_db[35:38, 35:38] = -16.0
    hh_db[35:38, 35:38] = -12.0

    # Target 2: Large Iceberg at (80:85, 80:85) -> 5x5 px
    hv_db[80:85, 80:85] = -14.0
    hh_db[80:85, 80:85] = -10.0

    # Target 3: Small Iceberg at (55:57, 95:97) -> 2x2 px
    hv_db[55:57, 95:97] = -17.5
    hh_db[55:57, 95:97] = -13.5

    ratio_db = hh_db - hv_db

    # Write 4-band COG using the real COGStackBuilder API
    scene_id = "S1B_EW_SLICE_TEST"
    transform = rasterio.transform.from_origin(2000000.0, 1000000.0, 40.0, 40.0)

    builder = COGStackBuilder(output_dir=tmp_path)
    cog_path = builder.build_and_export_cog(
        scene_id=scene_id,
        bands={
            "sigma0_hh_db": hh_db.astype(np.float32),
            "sigma0_hv_db": hv_db.astype(np.float32),
            "ratio_hh_hv": ratio_db.astype(np.float32),
            "incidence_angle": inc_deg.astype(np.float32),
        },
        transform=transform,
        crs="EPSG:3978",
    )

    return cog_path


def test_end_to_end_cfar_vertical_slice(synthetic_4band_cog: Path) -> None:
    """Execute complete vertical slice: COG -> CFAR -> Vectorize -> DB -> API GeoJSON."""
    # 1. Open COG and read bands
    with rasterio.open(synthetic_4band_cog) as src:
        assert src.count == 4
        assert str(src.crs) == "EPSG:3978"
        hh_db = src.read(1)
        hv_db = src.read(2)
        inc_deg = src.read(4)
        transform = src.transform
        bounds = src.bounds

    # 2. Execute CFAR detection
    detector = GammaCFARDetector(guard_window=(3, 3), background_window=(15, 15), pfa=1e-4)
    cfar_result = detector.detect(sigma0_hv_db=hv_db, sigma0_hh_db=hh_db)

    assert cfar_result.detection_mask.sum() > 0

    # 3. Vectorize hits to georeferenced targets
    vectorizer = TargetVectorizer(source_crs="EPSG:3978", target_crs="EPSG:4326", min_pixels=2)
    targets = vectorizer.extract_targets(
        detection_mask=cfar_result.detection_mask,
        transform=transform,
        sigma0_hv_db=hv_db,
        sigma0_hh_db=hh_db,
        incidence_angle=inc_deg,
        detector_name="Gamma-CFAR",
    )

    # We should have extracted the 3 injected icebergs
    assert len(targets) >= 3
    icebergs = [t for t in targets if t.predicted_class == "iceberg"]
    assert len(icebergs) >= 3

    # Check brightest iceberg metrics (Target 1)
    largest = max(icebergs, key=lambda t: t.peak_sigma0_hv_db)
    assert largest.pixel_area >= 1
    assert largest.peak_sigma0_hv_db >= -15.0

    # 4. Persist to SQLite Test DB and verify via FastAPI Client
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_db() -> Generator[Session, None, None]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db_session] = override_db
    client = TestClient(app)

    with session_factory() as session:
        poly_3978 = shapely.geometry.box(bounds.left, bounds.bottom, bounds.right, bounds.top)
        scene = SceneRepository.create_scene(
            session=session,
            product_id="S1B_EW_SLICE_TEST",
            platform="Sentinel-1B",
            mode="EW",
            polarizations=["HH", "HV"],
            acquisition_time=datetime.now(UTC),
            cog_path=str(synthetic_4band_cog),
            footprint_wgs84=poly_3978,
            status="DETECTED",
        )
        session.add(scene)
        session.commit()
        scene_id = scene.id

        for t in targets:
            DetectionRepository.create_detection(
                session=session,
                scene_id=scene.id,
                confidence=t.confidence,
                detector_name="Gamma-CFAR",
                predicted_class=t.predicted_class,
                geom_wgs84=t.geom_wgs84,
                centroid_wgs84=t.centroid_wgs84,
                length_m=t.length_m,
                width_m=t.width_m,
                estimated_area_m2=t.estimated_area_m2,
                peak_sigma0_hv_db=t.peak_sigma0_hv_db,
                mean_sigma0_hv_db=t.mean_sigma0_hv_db,
                peak_sigma0_hh_db=t.peak_sigma0_hh_db,
                hh_hv_ratio_db=t.hh_hv_ratio_db,
                incidence_angle_deg=t.incidence_angle_deg,
            )
        session.commit()

    # 5. Query via REST API
    client = TestClient(app)
    res = client.get("/api/v1/detections?scene_id=" + scene_id)
    assert res.status_code == 200
    geojson_data = res.json()
    assert geojson_data["type"] == "FeatureCollection"
    assert len(geojson_data["features"]) == len(targets)

    # 6. Test Analyst Validation API on one detection
    first_det_id = geojson_data["features"][0]["properties"]["id"]
    val_res = client.post(
        f"/api/v1/detections/{first_det_id}/validate",
        json={"analyst_verdict": "CONFIRMED_ICEBERG", "analyst_id": "analyst_lead"},
    )
    assert val_res.status_code == 201
    assert val_res.json()["analyst_verdict"] == "CONFIRMED_ICEBERG"
