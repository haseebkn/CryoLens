"""Unit tests for orbit file management and POEORB/RESORB timing checks."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cryolens.preprocess.orbits import OrbitManager, OrbitType


def test_orbit_type_selection_recent_scene_emits_warning(tmp_path: Path) -> None:
    """Verify that scenes newer than 21 days trigger a UserWarning and select RESORB."""
    mgr = OrbitManager(cache_dir=tmp_path)
    recent_acq = datetime.now(UTC) - timedelta(days=5)

    with pytest.warns(UserWarning, match="POEORB orbit file is not yet available"):
        orbit_type = mgr.determine_orbit_type(recent_acq, preference="POEORB")

    assert orbit_type == OrbitType.RESORB


def test_orbit_type_selection_historical_scene(tmp_path: Path) -> None:
    """Verify that scenes older than 21 days select POEORB without warning."""
    mgr = OrbitManager(cache_dir=tmp_path)
    historical_acq = datetime.now(UTC) - timedelta(days=45)

    orbit_type = mgr.determine_orbit_type(historical_acq, preference="POEORB")
    assert orbit_type == OrbitType.POEORB


def test_get_orbit_file_caching(tmp_path: Path) -> None:
    """Verify orbit file caching and metadata return."""
    mgr = OrbitManager(cache_dir=tmp_path)
    acq_dt = datetime(2023, 5, 10, 12, 0, 0, tzinfo=UTC)

    orbit_info = mgr.get_orbit_file("Sentinel-1A", acq_dt, orbit_type=OrbitType.POEORB)
    assert orbit_info["orbit_type"] == "POEORB"
    assert orbit_info["platform"] == "S1A"
    assert orbit_info["is_precise"] is True
    assert Path(orbit_info["orbit_file_path"]).exists()
