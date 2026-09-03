"""End-to-end detection chain over a real Sentinel-1 EW scene.

Skips when the AI4Arctic archive is absent, so CI stays green without an
11 GB download, but runs the genuine article locally: real dual-pol backscatter,
real ice charts, real land-distance zonation.

These assertions are physical rather than golden-value. They check that σ⁰
lands in the right decibel regime, that masking removes what it claims to, and
that suppression conserves the candidate ledger — properties that must hold for
any correct implementation, not values that must be frozen.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from cryolens.data.ai4arctic import build_scene_index, load_scene, scenes_intersecting_aoi
from cryolens.detect.filters import SuppressionConfig, build_analysis_mask, filter_targets
from cryolens.detect.runner import SceneDetectionRunner, classify_ice_regime

ARCHIVE = Path("data/raw/ai4arctic")
NL_BBOX = (-64.5, 42.5, -44.0, 60.5)

pytestmark = pytest.mark.skipif(
    not ARCHIVE.is_dir() or not any(ARCHIVE.rglob("*_prep.nc")),
    reason="AI4Arctic archive not present; run the dataset download first.",
)


@pytest.fixture(scope="module")
def scene_path() -> Path:
    """The first indexed scene whose centre falls inside the NL area of interest."""
    extents = build_scene_index(ARCHIVE)
    selected = scenes_intersecting_aoi(extents, NL_BBOX, require_centre=True)
    if not selected:
        pytest.skip("No archived scene falls inside the Newfoundland & Labrador AOI.")
    return selected[0].path


@pytest.fixture(scope="module")
def scene(scene_path: Path):  # noqa: ANN201 - fixture type is the loader's return
    """Load one real scene once and share it across the module."""
    return load_scene(scene_path)


class TestPhysicalUnits:
    """Restored backscatter must sit in the regime real S1 EW data occupies."""

    def test_cross_pol_is_physically_plausible(self, scene) -> None:  # noqa: ANN001
        """Median HV must land in the regime real S1 EW cross-pol occupies.

        This is the Phase 1 acceptance criterion from the project plan, made
        regime-aware. Calm open water sits near -33 dB, but sea ice
        volume-scatters strongly and pushes cross-pol up towards -20 dB, so a
        single threshold would fail on ice-affected Labrador scenes for entirely
        physical reasons. The open-water bound is therefore asserted only when
        the scene's own ice chart says it is open water.
        """
        median_hv = float(np.nanmedian(scene.sigma0_hv_db))
        assert -60.0 < median_hv < -15.0, f"HV median {median_hv:.1f} dB is outside the S1 EW range"

        regime, ice_fraction = classify_ice_regime(scene)
        if regime == "open_water":
            assert ice_fraction is not None
            assert median_hv < -25.0, (
                f"open-water scene (ice fraction {ice_fraction:.2f}) has HV median "
                f"{median_hv:.1f} dB, which is too bright for calm ocean"
            )

    def test_copol_exceeds_crosspol(self, scene) -> None:  # noqa: ANN001
        assert float(np.nanmedian(scene.sigma0_hh_db)) > float(np.nanmedian(scene.sigma0_hv_db))

    def test_incidence_within_ew_swath_limits(self, scene) -> None:  # noqa: ANN001
        inc = scene.incidence_angle_deg
        assert 15.0 <= float(np.nanmin(inc)) <= 25.0
        assert 40.0 <= float(np.nanmax(inc)) <= 50.0

    def test_land_distance_zones_are_in_range(self, scene) -> None:  # noqa: ANN001
        zones = scene.land_distance_zone
        assert int(zones.min()) >= 0
        assert int(zones.max()) <= 41

    def test_geolocation_lands_in_the_north_atlantic(self, scene) -> None:  # noqa: ANN001
        assert 40.0 < float(scene.latitude.min()) < 70.0
        assert -75.0 < float(scene.longitude.min()) < -40.0

    def test_assumptions_are_recorded(self, scene) -> None:  # noqa: ANN001
        """Non-recoverable variables must carry their caveat on the scene."""
        assert "incidence_angle" in scene.assumptions


class TestMasking:
    """The analysis mask must remove what it reports removing."""

    def test_mask_excludes_land_and_retains_most_water(self, scene) -> None:  # noqa: ANN001
        cfg = SuppressionConfig()
        mask, breakdown = build_analysis_mask(
            valid_mask=scene.valid_mask,
            land_distance_zone=scene.land_distance_zone,
            sic_class=scene.sic_class,
            sigma0_hv_db=scene.sigma0_hv_db,
            config=cfg,
        )
        assert not mask[scene.land_mask].any(), "no land pixel may survive masking"
        assert 0.3 < mask.mean() < 1.0, "masking should not consume most of the swath"
        assert sum(breakdown.values()) < 0.7

    def test_seam_masking_respects_its_ceiling(self, scene) -> None:  # noqa: ANN001
        cfg = SuppressionConfig(max_seam_fraction=0.05, seam_exclusion_px=8)
        _, breakdown = build_analysis_mask(
            valid_mask=scene.valid_mask,
            land_distance_zone=scene.land_distance_zone,
            sic_class=None,
            sigma0_hv_db=scene.sigma0_hv_db,
            config=cfg,
        )
        # Each flagged column is widened by +/- seam_exclusion_px, so the area
        # removed can exceed the column fraction, but not without bound.
        assert breakdown.get("subswath_seams", 0.0) < 0.30


class TestDetectionChain:
    """The full detect-and-suppress pass over a real swath."""

    @pytest.fixture(scope="class")
    @classmethod
    def result(cls, scene):  # noqa: ANN001, ANN206
        """Run the chain once for the whole class; a full swath takes ~40 s."""
        return SceneDetectionRunner(detector_kind="gamma", pfa=1e-5).run(scene)

    def test_produces_a_finite_detection_set(self, result) -> None:  # noqa: ANN001
        assert result.raw_candidates > 0, "CFAR should find candidates on a real swath"
        assert len(result.targets) <= result.raw_candidates

    def test_suppression_ledger_is_conserved(self, result) -> None:  # noqa: ANN001
        assert len(result.targets) + result.suppression.total_removed == result.raw_candidates

    def test_density_is_operationally_plausible(self, result) -> None:  # noqa: ANN001
        """Post-suppression density must be far below the raw candidate rate.

        The absolute value is an upper bound on the false-alarm rate, not a
        measured FAR (no iceberg ground truth exists), so the assertion is a
        loose plausibility band rather than a frozen number.
        """
        assert result.analysed_area_km2 > 1000.0
        assert result.detections_per_1000km2 < 10.0
        assert result.detections_per_1000km2 < result.raw_candidates_per_1000km2

    def test_targets_carry_geography_and_radiometry(self, result) -> None:  # noqa: ANN001
        if not result.targets:
            pytest.skip("No targets survived suppression on this scene.")
        t = result.targets[0]
        assert -75.0 < t.centroid_wgs84.x < -40.0
        assert 40.0 < t.centroid_wgs84.y < 70.0
        assert t.length_m > 0.0 and t.width_m > 0.0
        assert np.isfinite(t.peak_sigma0_hv_db)

    def test_regimes_are_classified(self, result, scene) -> None:  # noqa: ANN001
        regime, fraction = classify_ice_regime(scene)
        assert regime in {"open_water", "ice_affected", "unknown"}
        if regime == "unknown":
            # Challenge test scenes ship with their ice labels withheld; the
            # regime must stay unknown rather than defaulting to open water.
            assert fraction is None
        else:
            assert fraction is not None and 0.0 <= fraction <= 1.0
        assert result.wind_regime in {"low", "moderate", "high", "unknown"}


class TestSuppressionEffect:
    """Suppression must measurably reduce the candidate count on real data."""

    def test_filters_remove_speckle_dominated_candidates(self, scene) -> None:  # noqa: ANN001
        runner = SceneDetectionRunner(detector_kind="gamma", pfa=1e-5)
        result = runner.run(scene)

        unfiltered, _ = filter_targets(
            result.targets, SuppressionConfig(min_target_pixels=1, min_contrast_db=-99.0)
        )
        assert len(unfiltered) >= len(result.targets)
        if result.raw_candidates > 100:
            assert result.suppression_factor > 2.0
