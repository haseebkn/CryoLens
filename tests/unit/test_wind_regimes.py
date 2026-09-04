"""Unit tests for cohort-relative wind regime assignment.

Regression coverage for a stratification bug that produced a table saying
nothing: wind regimes were assigned by comparing a scene's median wind against
that same scene's own 33rd and 67th percentiles. A median lies between its own
terciles by construction, so all 39 benchmark scenes came out "moderate".
"""

from __future__ import annotations

import pytest

from cryolens.detect.filters import SuppressionStats
from cryolens.detect.runner import SceneDetectionResult, assign_wind_regimes


def _result(scene_id: str, wind: float | None) -> SceneDetectionResult:
    """Build a result carrying only the fields wind binning touches."""
    return SceneDetectionResult(
        scene_id=scene_id,
        detector_name="GammaCFARDetector",
        pfa=1e-5,
        targets=[],
        raw_pixel_hits=0,
        raw_candidates=0,
        analysed_area_km2=1000.0,
        suppression=SuppressionStats(),
        mask_breakdown={},
        ice_regime="open_water",
        sea_ice_fraction=0.0,
        wind_regime="unknown",
        wind_statistic=wind,
        runtime_s=0.0,
    )


class TestAssignWindRegimes:
    """Binning must separate scenes, not collapse them into one bucket."""

    def test_spread_cohort_uses_all_three_regimes(self) -> None:
        results = [_result(f"s{i}", float(i)) for i in range(9)]
        assign_wind_regimes(results)

        regimes = {r.wind_regime for r in results}
        assert regimes == {"low", "moderate", "high"}

    def test_ordering_is_respected(self) -> None:
        results = [_result(f"s{i}", float(i)) for i in range(9)]
        assign_wind_regimes(results)

        by_id = {r.scene_id: r.wind_regime for r in results}
        assert by_id["s0"] == "low"
        assert by_id["s8"] == "high"

    def test_does_not_collapse_to_one_bucket(self) -> None:
        """The exact failure mode of the original implementation."""
        results = [_result(f"s{i}", 0.1 * i) for i in range(39)]
        assign_wind_regimes(results)

        counts: dict[str, int] = {}
        for r in results:
            counts[r.wind_regime] = counts.get(r.wind_regime, 0) + 1
        assert len(counts) == 3, f"expected three regimes, got {counts}"
        assert max(counts.values()) < len(results), "no regime may contain every scene"

    def test_scenes_without_wind_stay_unknown(self) -> None:
        results = [_result(f"s{i}", float(i)) for i in range(6)]
        results.append(_result("no_wind", None))
        assign_wind_regimes(results)

        assert results[-1].wind_regime == "unknown"
        assert all(r.wind_regime != "unknown" for r in results[:-1])

    def test_missing_wind_does_not_shift_boundaries(self) -> None:
        """Scenes without wind data must be excluded from the tercile fit."""
        measured = [_result(f"s{i}", float(i)) for i in range(9)]
        with_gaps = [_result(f"s{i}", float(i)) for i in range(9)]
        with_gaps += [_result(f"n{i}", None) for i in range(20)]

        assign_wind_regimes(measured)
        assign_wind_regimes(with_gaps)

        assert [r.wind_regime for r in measured] == [r.wind_regime for r in with_gaps[:9]]

    def test_too_few_measured_scenes_reports_unknown(self) -> None:
        results = [_result("a", 1.0), _result("b", 2.0)]
        assign_wind_regimes(results)
        assert all(r.wind_regime == "unknown" for r in results)

    def test_constant_wind_cohort_does_not_crash(self) -> None:
        results = [_result(f"s{i}", 5.0) for i in range(5)]
        assign_wind_regimes(results)
        # With no spread every scene sits on both boundaries; the low branch
        # wins, and the important property is that it terminates cleanly.
        assert {r.wind_regime for r in results} == {"low"}

    def test_empty_input_is_safe(self) -> None:
        assign_wind_regimes([])

    def test_statistic_is_serialised(self) -> None:
        r = _result("s", 1.23456789)
        assert r.to_dict()["wind_statistic"] == pytest.approx(1.23457, abs=1e-5)
        assert _result("s", None).to_dict()["wind_statistic"] is None
