"""Tests for public-mirror orbit acquisition.

Sentinel-1 orbit products are official ESA auxiliary data served by read-only
public mirrors, so acquiring one needs no CDSE or Earthdata account even though
imagery download does. These tests are hermetic: the HTTP layer is substituted,
so they assert selection and validation logic rather than mirror availability.

The distinction the tests defend most carefully is that **downloading an orbit
is not applying one**. A cached precise orbit says nothing about the geolocation
in a product annotation, and provenance must keep saying so until a correction
is actually performed and verified.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cryolens.preprocess.orbits import (
    ASF_AUX_POEORB,
    ESA_STEP_AUXDATA,
    OrbitManager,
    OrbitType,
    covers,
    parse_eof_name,
)

ACQUISITION = datetime(2018, 4, 28, 9, 39, 37, tzinfo=UTC)
COVERING = "S1B_OPER_AUX_POEORB_OPOD_20210313T001817_V20180427T225942_20180429T005942.EOF"
NEWER_DUPLICATE = "S1B_OPER_AUX_POEORB_OPOD_20220101T000000_V20180427T225942_20180429T005942.EOF"
NON_COVERING = "S1B_OPER_AUX_POEORB_OPOD_20180510T110554_V20180419T225942_20180421T005942.EOF"


def _orbit_xml(n_vectors: int = 4) -> bytes:
    """A minimal but structurally valid EOF document."""
    vectors = "".join(
        "<OSV>"
        f"<UTC>UTC=2018-04-28T{9 + i:02d}:00:00.000000</UTC>"
        "<X>1.0</X><Y>2.0</Y><Z>3.0</Z><VX>4.0</VX><VY>5.0</VY><VZ>6.0</VZ>"
        "</OSV>"
        for i in range(n_vectors)
    )
    return (
        "<Earth_Explorer_File><Earth_Explorer_Header><Fixed_Header>"
        "<Mission>Sentinel-1B</Mission><File_Type>AUX_POEORB</File_Type>"
        "</Fixed_Header></Earth_Explorer_Header>"
        f"<Data_Block><List_of_OSVs>{vectors}</List_of_OSVs></Data_Block>"
        "</Earth_Explorer_File>"
    ).encode()


class _Response:
    """Stand-in for a requests Response."""

    def __init__(self, *, text: str = "", content: bytes = b"", status: int = 200) -> None:
        self.text = text
        self.content = content
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[str], _Response]
) -> list[str]:
    """Route requests.get through ``handler``; returns the recorded URL list."""
    seen: list[str] = []

    def fake_get(url: str, timeout: float = 0.0) -> _Response:
        seen.append(url)
        return handler(url)

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


class TestFilenameParsing:
    """Validity windows come from the filename, not from a directory's name."""

    def test_parses_a_real_product_name(self) -> None:
        entry = parse_eof_name(COVERING)
        assert entry is not None
        assert entry["platform"] == "S1B"
        assert entry["orbit_type"] == "POEORB"
        assert entry["validity_start"] == datetime(2018, 4, 27, 22, 59, 42, tzinfo=UTC)
        assert entry["validity_stop"] == datetime(2018, 4, 29, 0, 59, 42, tzinfo=UTC)

    def test_parses_zipped_variant(self) -> None:
        assert parse_eof_name(COVERING + ".zip") is not None

    def test_rejects_unrelated_names(self) -> None:
        assert parse_eof_name("index.html") is None
        assert parse_eof_name("S1B_OPER_AUX_CAL_.EOF") is None

    def test_coverage_window_is_inclusive(self) -> None:
        entry = parse_eof_name(COVERING)
        assert entry is not None
        assert covers(entry, ACQUISITION)
        assert covers(entry, entry["validity_start"])
        assert covers(entry, entry["validity_stop"])
        assert not covers(entry, entry["validity_stop"] + timedelta(seconds=1))

    def test_non_covering_product_is_rejected(self) -> None:
        entry = parse_eof_name(NON_COVERING)
        assert entry is not None
        assert not covers(entry, ACQUISITION)


class TestPlatformNormalisation:
    """Mission codes arrive in several spellings across catalogues."""

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("S1A", "S1A"), ("Sentinel-1B", "S1B"), ("sentinel-1c", "S1C"), ("S1D", "S1D")],
    )
    def test_accepts_known_platforms(self, given: str, expected: str) -> None:
        assert OrbitManager._platform_code(given) == expected

    def test_rejects_unknown_platform(self) -> None:
        with pytest.raises(ValueError, match="Unknown Sentinel-1 platform"):
            OrbitManager._platform_code("RADARSAT-2")


class TestListingUrls:
    """A month-partitioned mirror must be probed either side of a boundary."""

    def test_includes_neighbouring_months(self, tmp_path: Path) -> None:
        manager = OrbitManager(cache_dir=tmp_path)
        # An acquisition just after midnight on the 1st: the covering product
        # is filed under the previous month.
        urls = manager._candidate_listings(
            "S1B", datetime(2018, 5, 1, 0, 10, tzinfo=UTC), OrbitType.POEORB
        )
        assert any("2018/04/" in u for u in urls)
        assert any("2018/05/" in u for u in urls)

    def test_prefers_esa_then_falls_back_to_asf(self, tmp_path: Path) -> None:
        urls = OrbitManager(cache_dir=tmp_path)._candidate_listings(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert urls[0].startswith(ESA_STEP_AUXDATA)
        assert urls[-1] == ASF_AUX_POEORB

    def test_no_duplicate_listings(self, tmp_path: Path) -> None:
        urls = OrbitManager(cache_dir=tmp_path)._candidate_listings(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert len(urls) == len(set(urls))


class TestDownloadSelection:
    """The right product must be chosen, and a wrong one never cached."""

    def test_downloads_the_covering_product(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(text=f'<a href="{NON_COVERING}">a</a><a href="{COVERING}">b</a>')
            return _Response(content=_orbit_xml())

        _install_transport(monkeypatch, handler)
        path = OrbitManager(cache_dir=tmp_path).download_orbit_file(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert path.name == COVERING
        assert path.is_file()

    def test_prefers_most_recently_generated_for_same_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(
                    text=f'<a href="{COVERING}">a</a><a href="{NEWER_DUPLICATE}">b</a>'
                )
            return _Response(content=_orbit_xml())

        _install_transport(monkeypatch, handler)
        path = OrbitManager(cache_dir=tmp_path).download_orbit_file(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert path.name == NEWER_DUPLICATE

    def test_unzips_esa_archive_members(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(COVERING, _orbit_xml())

        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(text=f'<a href="{COVERING}.zip">z</a>')
            return _Response(content=buffer.getvalue())

        _install_transport(monkeypatch, handler)
        path = OrbitManager(cache_dir=tmp_path).download_orbit_file(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert path.read_bytes().startswith(b"<Earth_Explorer_File>")

    def test_reuses_cached_product_without_refetching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / COVERING).write_bytes(_orbit_xml())

        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(text=f'<a href="{COVERING}">a</a>')
            raise AssertionError("cached orbit must not be downloaded again")

        _install_transport(monkeypatch, handler)
        path = OrbitManager(cache_dir=tmp_path).download_orbit_file(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert path.name == COVERING

    def test_raises_when_nothing_covers_the_acquisition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_transport(
            monkeypatch, lambda url: _Response(text=f'<a href="{NON_COVERING}">a</a>')
        )
        with pytest.raises(FileNotFoundError, match="No published POEORB orbit covers"):
            OrbitManager(cache_dir=tmp_path).download_orbit_file(
                "S1B", ACQUISITION, OrbitType.POEORB
            )
        assert not list(tmp_path.iterdir()), "a failed lookup must not leave a file behind"


class TestDownloadValidation:
    """A mirror returning junk must never populate the cache."""

    def test_rejects_non_xml_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(text=f'<a href="{COVERING}">a</a>')
            return _Response(content=b"<html>503 Service Unavailable</html>")

        _install_transport(monkeypatch, handler)
        with pytest.raises(FileNotFoundError):
            OrbitManager(cache_dir=tmp_path).download_orbit_file(
                "S1B", ACQUISITION, OrbitType.POEORB
            )
        assert not list(tmp_path.iterdir())

    def test_rejects_orbit_without_state_vectors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(text=f'<a href="{COVERING}">a</a>')
            return _Response(content=_orbit_xml(n_vectors=1))

        _install_transport(monkeypatch, handler)
        with pytest.raises(FileNotFoundError):
            OrbitManager(cache_dir=tmp_path).download_orbit_file(
                "S1B", ACQUISITION, OrbitType.POEORB
            )
        assert not list(tmp_path.iterdir())

    def test_falls_through_to_next_mirror_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(url: str) -> _Response:
            if url.startswith(ESA_STEP_AUXDATA):
                return _Response(status=503)
            if url.endswith("/"):
                return _Response(text=f'<a href="{COVERING}">a</a>')
            return _Response(content=_orbit_xml())

        seen = _install_transport(monkeypatch, handler)
        path = OrbitManager(cache_dir=tmp_path).download_orbit_file(
            "S1B", ACQUISITION, OrbitType.POEORB
        )
        assert path.is_file()
        assert any(u.startswith(ASF_AUX_POEORB) for u in seen)


class TestProvenanceHonesty:
    """Acquiring an orbit must never be recorded as having applied one."""

    def test_downloaded_orbit_is_not_marked_as_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(url: str) -> _Response:
            if url.endswith("/"):
                return _Response(text=f'<a href="{COVERING}">a</a>')
            return _Response(content=_orbit_xml())

        _install_transport(monkeypatch, handler)
        manager = OrbitManager(cache_dir=tmp_path)
        manager.download_orbit_file("S1B", ACQUISITION, OrbitType.POEORB)

        info = manager.get_orbit_file("Sentinel-1B", ACQUISITION, OrbitType.POEORB)
        assert info["is_precise"] is True
        assert info["orbit_correction_applied"] is False, (
            "a cached precise orbit does not change the annotated geolocation; "
            "provenance must not imply a correction that was never performed"
        )
