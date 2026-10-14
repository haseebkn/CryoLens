"""Unit tests for International Ice Patrol sighting ingestion.

Regression coverage for a call-signature mismatch that made ingestion raise
``TypeError`` on every row: the client passed prebuilt Shapely geometries to a
repository method that takes scalar longitude and latitude and builds both CRS
geometries itself in PostGIS.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cryolens.db.repositories import IIPSightingRepository
from cryolens.ingest.iip import IIPClient


class _RecordingRepository:
    """Captures create_sighting calls instead of touching a database."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_sighting(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def iip_csv(tmp_path: Path) -> Path:
    """A small IIP-style CSV covering both supported date formats."""
    path = tmp_path / "iip.csv"
    path.write_text(
        "SIGHTING_DATE,SIGHTING_TIME,LATITUDE,LONGITUDE,SIZE,SHAPE\n"
        "04/15/2020,1230,48.5,-52.3,Medium,Tabular\n"
        "2020-04-16,0600,47.9,51.8,Large,Non-Tabular\n",
        encoding="utf-8",
    )
    return path


class TestCallSignature:
    """The client must call the repository the way the repository is defined."""

    def test_client_matches_repository_signature(self) -> None:
        params = set(inspect.signature(IIPSightingRepository.create_sighting).parameters)
        # The repository derives both geometries from scalars; passing prebuilt
        # geometry keywords is what previously broke ingestion.
        assert {"lon", "lat"} <= params
        assert "geom_epsg3978" not in params
        assert "geom_wgs84" not in params

    def test_client_does_not_import_undeclared_loguru(self) -> None:
        source = Path("src/cryolens/ingest/iip.py").read_text(encoding="utf-8")
        assert "loguru" not in source, "loguru is not a declared dependency"


class TestIngestion:
    """Parsing behaviour over representative IIP rows."""

    def test_parses_both_date_formats(self, iip_csv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingRepository()
        monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder, raising=True)

        class _Session:
            def commit(self) -> None:
                pass

        count = IIPClient().ingest_csv(_Session(), iip_csv)  # type: ignore[arg-type]

        assert count == 2
        assert len(recorder.calls) == 2
        first, second = recorder.calls
        assert first["sighting_time"] == datetime(2020, 4, 15, 12, 30, tzinfo=UTC)
        assert second["sighting_time"] == datetime(2020, 4, 16, 6, 0, tzinfo=UTC)

    def test_western_longitude_sign_is_corrected(
        self, iip_csv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IIP files sometimes record west longitude as a positive number.

        The Grand Banks and Labrador Shelf lie between 44 and 65 degrees west,
        so a positive value in that band is unambiguous and is negated.
        """
        recorder = _RecordingRepository()
        monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder, raising=True)

        class _Session:
            def commit(self) -> None:
                pass

        IIPClient().ingest_csv(_Session(), iip_csv)  # type: ignore[arg-type]

        assert recorder.calls[0]["lon"] == pytest.approx(-52.3)
        # The second row is written as +51.8 and must be flipped to west.
        assert recorder.calls[1]["lon"] == pytest.approx(-51.8)

    def test_attributes_are_forwarded(self, iip_csv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingRepository()
        monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder, raising=True)

        class _Session:
            def commit(self) -> None:
                pass

        IIPClient().ingest_csv(_Session(), iip_csv)  # type: ignore[arg-type]

        assert recorder.calls[0]["size_class"] == "Medium"
        assert recorder.calls[0]["shape"] == "Tabular"
        assert "iip.csv" in str(recorder.calls[0]["source"])

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            IIPClient().ingest_csv(None, tmp_path / "absent.csv")  # type: ignore[arg-type]


def test_colon_time_and_invalid_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingRepository()
    monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder)
    path = tmp_path / "sightings.csv"
    path.write_text(
        "DATE,TIME,LATITUDE,LONGITUDE\n2020-04-15,12:30,48.5,-52.3\n2020-04-15,12:30,nan,-52.3\n2020-04-15,12:30,70,-130\n"
    )
    from unittest.mock import Mock

    assert IIPClient().ingest_csv(Mock(), path) == 1
    assert recorder.calls[0]["sighting_time"] == datetime(2020, 4, 15, 12, 30, tzinfo=UTC)


def test_publisher_csv_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute ingestion on an attributed real publisher excerpt, with no database."""
    from unittest.mock import Mock

    recorder = _RecordingRepository()
    monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder)
    path = Path(__file__).parents[1] / "data/iip-2021-publisher-excerpt.csv"
    session = Mock()
    assert IIPClient().ingest_csv(session, path) == 1
    assert recorder.calls[0]["lat"] == pytest.approx(55.19833)
    assert recorder.calls[0]["lon"] == pytest.approx(-58.195)
    assert recorder.calls[0]["sighting_time"] == datetime(2020, 10, 1, 21, 33, tzinfo=UTC)
    session.commit.assert_called_once()


@pytest.mark.parametrize(
    "header",
    [
        "DATE,TIME,LATITUDE",
        "DATE,TIME,FOO,BAR",
        "DATE,TIME,LATITUDE,LATITUDE,LONGITUDE",
        "DATE,TIME,LATITUDE,SIGHTING_LATITUDE,LONGITUDE",
        "",
    ],
)
def test_invalid_schema_fails_before_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    from unittest.mock import Mock

    recorder = _RecordingRepository()
    monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder)
    path = tmp_path / "invalid.csv"
    path.write_text(header + "\n", encoding="utf-8")
    session = Mock()
    with pytest.raises(ValueError, match="header"):
        IIPClient().ingest_csv(session, path)
    assert recorder.calls == []
    session.commit.assert_not_called()


def test_publisher_header_case_and_whitespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    recorder = _RecordingRepository()
    monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder)
    path = tmp_path / "case.csv"
    path.write_text(
        " sighting_date ,sighting_time,Sighting_Latitude, sighting_longitude \n"
        "2020-04-15,12:30,48.5,-52.3\n",
        encoding="utf-8-sig",
    )
    assert IIPClient().ingest_csv(Mock(), path) == 1
    assert recorder.calls[0]["lon"] == pytest.approx(-52.3)


def test_unparseable_rows_fail_instead_of_reporting_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from unittest.mock import Mock

    recorder = _RecordingRepository()
    monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder)
    path = tmp_path / "bad-rows.csv"
    path.write_text("DATE,TIME,LATITUDE,LONGITUDE\n2020-04-15,,48.5,-52.3\n", encoding="utf-8")
    session = Mock()
    with pytest.raises(ValueError, match="no parseable sightings"):
        IIPClient().ingest_csv(session, path)
    assert recorder.calls == []
    session.commit.assert_not_called()
    assert "malformed IIP CSV row 2" in caplog.text


def test_valid_outside_nl_is_not_a_schema_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    recorder = _RecordingRepository()
    monkeypatch.setattr("cryolens.ingest.iip.IIPSightingRepository", recorder)
    path = tmp_path / "outside.csv"
    path.write_text("DATE,TIME,LATITUDE,LONGITUDE\n2020-04-15,1230,70,-130\n", encoding="utf-8")
    assert IIPClient().ingest_csv(Mock(), path) == 0
    assert recorder.calls == []
