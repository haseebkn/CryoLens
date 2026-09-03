"""Unit tests for the false-alarm suppression chain."""

from __future__ import annotations

import numpy as np
import pytest
import shapely.geometry

from cryolens.detect.filters import (
    SuppressionConfig,
    SuppressionStats,
    build_analysis_mask,
    deduplicate_across_tiles,
    detect_subswath_seams,
    filter_targets,
)
from cryolens.geo.vectorize import ExtractedTarget


def _target(
    target_id: int = 1,
    pixel_area: int = 10,
    length_m: float = 200.0,
    width_m: float = 150.0,
    peak_hv: float = -20.0,
    ratio_db: float = 8.0,
    lon: float = -52.0,
    lat: float = 47.0,
    confidence: float = 0.8,
    bbox: tuple[int, int, int, int] = (10, 10, 14, 14),
) -> ExtractedTarget:
    """Build a minimal ExtractedTarget for filter testing."""
    centroid_wgs84 = shapely.geometry.Point(lon, lat)
    poly_wgs84 = shapely.geometry.box(lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01)
    # Use a metric-like planar stand-in so distances are predictable in tests.
    centroid_proj = shapely.geometry.Point(lon * 1000.0, lat * 1000.0)
    poly_proj = shapely.geometry.box(
        lon * 1000.0 - 50.0, lat * 1000.0 - 50.0, lon * 1000.0 + 50.0, lat * 1000.0 + 50.0
    )
    return ExtractedTarget(
        target_id=target_id,
        geom_epsg3978=poly_proj,
        geom_wgs84=poly_wgs84,
        centroid_wgs84=centroid_wgs84,
        centroid_epsg3978=centroid_proj,
        pixel_bbox=bbox,
        pixel_area=pixel_area,
        length_m=length_m,
        width_m=width_m,
        estimated_area_m2=float(pixel_area) * 6400.0,
        peak_sigma0_hv_db=peak_hv,
        mean_sigma0_hv_db=peak_hv - 2.0,
        peak_sigma0_hh_db=peak_hv + ratio_db,
        mean_sigma0_hh_db=peak_hv + ratio_db - 2.0,
        hh_hv_ratio_db=ratio_db,
        incidence_angle_deg=35.0,
        predicted_class="iceberg",
        confidence=confidence,
    )


class TestSuppressionStats:
    """The suppression ledger must account for every removed candidate."""

    def test_records_and_totals(self) -> None:
        stats = SuppressionStats()
        stats.record("min_size", 100, 20)
        stats.record("aspect_ratio", 5, 15)
        assert stats.total_removed == 105
        assert stats.as_dict()["stages"][0]["stage"] == "min_size"
        assert "min_size" in stats.format_table()

    def test_empty_ledger_formats(self) -> None:
        assert "no suppression" in SuppressionStats().format_table()


class TestAnalysisMask:
    """Pre-detection masking decides where clutter statistics may be estimated."""

    def test_land_and_coastal_buffer_removed(self) -> None:
        valid = np.ones((100, 100), dtype=bool)
        zones = np.full((100, 100), 40, dtype=np.int16)
        zones[:20, :] = 0  # land
        zones[20:25, :] = 2  # coastal zones within the buffer

        cfg = SuppressionConfig(coastal_buffer_zones=2, border_exclusion_px=0, seam_detection_enabled=False)
        mask, breakdown = build_analysis_mask(valid, zones, None, None, cfg)

        assert not mask[:25, :].any(), "land and buffered coast must be excluded"
        assert mask[30:, :].all()
        assert breakdown["land_and_coastal_buffer"] == pytest.approx(0.25, abs=1e-6)

    def test_border_exclusion(self) -> None:
        valid = np.ones((50, 50), dtype=bool)
        cfg = SuppressionConfig(border_exclusion_px=5, seam_detection_enabled=False)
        mask, breakdown = build_analysis_mask(valid, None, None, None, cfg)

        assert not mask[:5, :].any() and not mask[-5:, :].any()
        assert not mask[:, :5].any() and not mask[:, -5:].any()
        assert mask[10:40, 10:40].all()
        assert breakdown["swath_border"] > 0.0

    def test_sea_ice_retained_unless_requested(self) -> None:
        valid = np.ones((40, 40), dtype=bool)
        sic = np.zeros((40, 40), dtype=np.uint8)
        sic[:20, :] = 8  # 80 percent ice

        keep = SuppressionConfig(border_exclusion_px=0, seam_detection_enabled=False, exclude_sea_ice=False)
        mask_keep, _ = build_analysis_mask(valid, None, sic, None, keep)
        assert mask_keep.all(), "ice must be retained by default so it can be measured"

        drop = SuppressionConfig(border_exclusion_px=0, seam_detection_enabled=False, exclude_sea_ice=True)
        mask_drop, breakdown = build_analysis_mask(valid, None, sic, None, drop)
        assert not mask_drop[:20, :].any()
        assert breakdown["sea_ice"] == pytest.approx(0.5, abs=1e-6)

    def test_invalid_pixels_never_enter_the_mask(self) -> None:
        valid = np.ones((30, 30), dtype=bool)
        valid[5:10, 5:10] = False
        cfg = SuppressionConfig(border_exclusion_px=0, seam_detection_enabled=False)
        mask, _ = build_analysis_mask(valid, None, None, None, cfg)
        assert not mask[5:10, 5:10].any()


class TestSeamDetection:
    """Subswath seams appear as steps in the range-direction noise profile."""

    def test_finds_injected_step(self) -> None:
        rng = np.random.default_rng(0)
        hv = rng.normal(-33.0, 0.05, size=(200, 300))
        hv[:, 150:] += 3.0  # a noise-floor step, as between EW subswaths

        seams = detect_subswath_seams(hv, sigma=4.0)
        assert seams.any()
        assert seams[145:156].any(), "seam should be located near the injected step"

    def test_homogeneous_scene_has_no_seams(self) -> None:
        rng = np.random.default_rng(1)
        hv = rng.normal(-33.0, 0.05, size=(200, 300))
        seams = detect_subswath_seams(hv, sigma=8.0)
        assert seams.sum() <= 3, "a homogeneous scene should yield almost no seam columns"

    def test_all_nan_input_is_safe(self) -> None:
        hv = np.full((10, 10), np.nan)
        seams = detect_subswath_seams(hv)
        assert seams.shape == (10,)
        assert not seams.any()


class TestTargetFilters:
    """Post-detection gating rejects geometrically implausible candidates."""

    def test_min_size_removes_speckle_spikes(self) -> None:
        targets = [_target(1, pixel_area=1), _target(2, pixel_area=10)]
        kept, stats = filter_targets(targets, SuppressionConfig(min_target_pixels=4))
        assert [t.target_id for t in kept] == [2]
        assert stats.stages[0][0] == "min_size"
        assert stats.stages[0][1] == 1

    def test_max_size_removes_ice_floes(self) -> None:
        targets = [_target(1, pixel_area=50_000), _target(2, pixel_area=10)]
        kept, _ = filter_targets(targets, SuppressionConfig(max_target_pixels=20_000))
        assert [t.target_id for t in kept] == [2]

    def test_aspect_ratio_removes_streaks(self) -> None:
        streak = _target(1, length_m=2000.0, width_m=100.0)  # 20:1
        compact = _target(2, length_m=200.0, width_m=150.0)
        kept, _ = filter_targets([streak, compact], SuppressionConfig(max_aspect_ratio=8.0))
        assert [t.target_id for t in kept] == [2]

    def test_copol_dominance_rejected(self) -> None:
        specular = _target(1, ratio_db=25.0)
        volume = _target(2, ratio_db=8.0)
        kept, _ = filter_targets([specular, volume], SuppressionConfig(max_hh_hv_ratio_db=18.0))
        assert [t.target_id for t in kept] == [2]

    def test_dim_targets_rejected(self) -> None:
        dim = _target(1, peak_hv=-40.0)
        bright = _target(2, peak_hv=-18.0)
        kept, _ = filter_targets([dim, bright], SuppressionConfig(min_peak_hv_db=-30.0))
        assert [t.target_id for t in kept] == [2]

    def test_clutter_contrast_gate(self) -> None:
        clutter = np.full((40, 40), -20.0)
        # peak_hv -20 gives zero contrast against a -20 dB clutter floor.
        flat = _target(1, peak_hv=-20.0, bbox=(10, 10, 14, 14))
        strong = _target(2, peak_hv=-10.0, bbox=(10, 10, 14, 14))
        kept, stats = filter_targets(
            [flat, strong], SuppressionConfig(min_contrast_db=3.0), clutter_mean_db=clutter
        )
        assert [t.target_id for t in kept] == [2]
        assert any(s[0] == "clutter_contrast" for s in stats.stages)

    def test_ledger_is_conserved(self) -> None:
        targets = [_target(i, pixel_area=1 if i % 2 else 10) for i in range(1, 21)]
        kept, stats = filter_targets(targets, SuppressionConfig())
        assert len(kept) + stats.total_removed == len(targets)


class TestCrossTileDeduplication:
    """The same physical target seen in two overlapping tiles must collapse to one."""

    def test_near_duplicate_removed(self) -> None:
        a = _target(1, lon=-52.0, lat=47.0, confidence=0.9)
        b = _target(2, lon=-52.0, lat=47.0, confidence=0.6)
        kept = deduplicate_across_tiles([a, b], centroid_tolerance_m=150.0)
        assert len(kept) == 1
        assert kept[0].target_id == 1, "the higher-confidence detection survives"

    def test_distinct_targets_preserved(self) -> None:
        a = _target(1, lon=-52.0, lat=47.0)
        b = _target(2, lon=-51.0, lat=46.0)
        kept = deduplicate_across_tiles([a, b], centroid_tolerance_m=150.0)
        assert len(kept) == 2

    def test_single_and_empty_inputs(self) -> None:
        assert deduplicate_across_tiles([]) == []
        one = [_target(1)]
        assert len(deduplicate_across_tiles(one)) == 1
