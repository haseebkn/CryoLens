"""Tests for the published benchmark renderer.

The renderer is the last step before a number becomes a claim, so it carries two
responsibilities beyond formatting: it refuses unversioned legacy reports, and
it marks stratified rows that are too thin to interpret.

The second exists because the three-acquisition run split its wind terciles one
scene per bin, yielding per-stratum suppression factors between 11x and 582x.
Those describe individual scenes. Rendered without qualification they read as a
regime comparison, which is the kind of over-reading the rest of the reporting
is careful to avoid.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "make_benchmark_doc",
    Path(__file__).resolve().parents[2] / "scripts" / "make_benchmark_doc.py",
)
assert _SPEC is not None and _SPEC.loader is not None
make_benchmark_doc = importlib.util.module_from_spec(_SPEC)
sys.modules["make_benchmark_doc"] = make_benchmark_doc
_SPEC.loader.exec_module(make_benchmark_doc)

render = make_benchmark_doc.render
MIN_SCENES_PER_STRATUM = make_benchmark_doc.MIN_SCENES_PER_STRATUM


def _stratum(name: str, n_scenes: int, targets: int = 10) -> dict[str, Any]:
    """Build one stratified row."""
    return {
        "stratum": name,
        "n_scenes": n_scenes,
        "area_km2": 100_000.0,
        "targets": targets,
        "raw_candidates": targets * 10,
        "density_per_1000km2": targets / 100.0,
        "raw_density_per_1000km2": targets / 10.0,
        "suppression_factor": 10.0,
    }


def _report(**overrides: Any) -> dict[str, Any]:
    """A minimal schema-2 report that the renderer will accept."""
    report: dict[str, Any] = {
        "schema_version": 2,
        "detector": "Gamma-CFAR",
        "methodology": {"metric": "unverified_candidate_density_per_1000km2"},
        "run_manifests": [{"scene_id": "s1"}],
        "overall": {
            "stratum": "all",
            "n_scenes": 9,
            "area_km2": 900_000.0,
            "targets": 90,
            "raw_candidates": 900,
            "density_per_1000km2": 0.1,
            "raw_density_per_1000km2": 1.0,
            "suppression_factor": 10.0,
        },
        "by_ice_regime": [],
        "by_wind_regime": [],
        "suppression_ledger": [],
        "scenes": [],
    }
    report.update(overrides)
    return report


class TestSchemaGuard:
    """Legacy or incomplete reports must not be publishable as audited evidence."""

    def test_rejects_unversioned_report(self) -> None:
        bad = _report()
        del bad["schema_version"]
        with pytest.raises(ValueError, match="Legacy/unversioned"):
            render(bad)

    def test_rejects_missing_methodology(self) -> None:
        with pytest.raises(ValueError, match="Legacy/unversioned"):
            render(_report(methodology={}))

    def test_rejects_missing_run_manifests(self) -> None:
        with pytest.raises(ValueError, match="Legacy/unversioned"):
            render(_report(run_manifests=[]))

    def test_rejects_zero_coverage(self) -> None:
        overall = _report()["overall"] | {"area_km2": 0.0}
        with pytest.raises(ValueError, match="positive analyzed coverage"):
            render(_report(overall=overall))


class TestStratumInterpretability:
    """Thin strata must be marked, not presented alongside adequate ones."""

    def test_single_scene_stratum_is_flagged(self) -> None:
        out = render(_report(by_wind_regime=[_stratum("low", 1), _stratum("high", 1)]))
        assert f"**no — n<{MIN_SCENES_PER_STRATUM}**" in out
        assert "not evidence of a trend" in out

    def test_adequate_strata_are_not_flagged(self) -> None:
        out = render(
            _report(
                by_wind_regime=[
                    _stratum("low", 8),
                    _stratum("moderate", 9),
                    _stratum("high", 8),
                ]
            )
        )
        assert f"n<{MIN_SCENES_PER_STRATUM}" not in out
        assert "not evidence of a trend" not in out

    def test_mixed_strata_name_only_the_thin_ones(self) -> None:
        out = render(_report(by_ice_regime=[_stratum("open_water", 7), _stratum("unknown", 1)]))
        caution = next(line for line in out.splitlines() if "not evidence of a trend" in line)
        assert "unknown" in caution
        assert "open_water" not in caution

    def test_threshold_is_inclusive(self) -> None:
        """A stratum with exactly the minimum count is interpretable."""
        out = render(_report(by_wind_regime=[_stratum("low", MIN_SCENES_PER_STRATUM)]))
        assert f"n<{MIN_SCENES_PER_STRATUM}" not in out

    def test_every_stratum_still_appears(self) -> None:
        """Flagging must not hide rows; the accounting has to stay complete."""
        out = render(_report(by_wind_regime=[_stratum("low", 1), _stratum("moderate", 5)]))
        assert "| low |" in out
        assert "| moderate |" in out


class TestReportContent:
    """Headline framing must survive any future edit to the renderer."""

    def test_states_precision_and_recall_are_unmeasured(self) -> None:
        out = render(_report())
        assert "Precision, recall and empirical false-positive rate are not measured" in out

    def test_warns_that_fewer_returns_may_be_missed_targets(self) -> None:
        out = render(_report())
        assert "missed real targets" in out

    def test_disclaims_operational_certification(self) -> None:
        out = render(_report())
        assert "No certified C-CORE operating point" in out
