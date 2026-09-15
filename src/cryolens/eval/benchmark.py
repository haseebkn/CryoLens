"""Exploratory benchmark harness for maritime SAR target detection.

What this measures, and what it does not
----------------------------------------
The AI4Arctic distribution carries ice charts, not iceberg point truth, and no
freely available dataset provides verified iceberg positions co-registered to
these acquisitions. This harness therefore reports the metric that *can* be
measured rigorously without label leakage:

    **detection density per 1000 square kilometres of analysed water**

stratified by sea ice regime and relative wind regime, together with the
suppression ledger showing where the candidate budget went.

Candidate density bounds false-candidate density by counting all candidates;
it is not a measured false-alarm probability or evidence of precision. Iceberg
prevalence and the number of missed objects are unknown.

IIP spatiotemporal association is implemented separately; it cannot establish
recall or turn a nearby candidate into independently verified ground truth.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cryolens.data.ai4arctic import SceneExtent, load_scene
from cryolens.detect.filters import SuppressionConfig
from cryolens.detect.runner import (
    SceneDetectionResult,
    SceneDetectionRunner,
    assign_wind_regimes,
)

logger = logging.getLogger(__name__)


@dataclass
class StratumSummary:
    """Aggregated statistics for one stratum of the benchmark."""

    name: str
    n_scenes: int
    total_area_km2: float
    total_targets: int
    total_raw_candidates: int

    @property
    def density_per_1000km2(self) -> float:
        """Area-weighted detection density."""
        if self.total_area_km2 <= 0.0:
            return 0.0
        return 1000.0 * self.total_targets / self.total_area_km2

    @property
    def raw_density_per_1000km2(self) -> float:
        """Area-weighted pre-suppression candidate density."""
        if self.total_area_km2 <= 0.0:
            return 0.0
        return 1000.0 * self.total_raw_candidates / self.total_area_km2

    @property
    def suppression_factor(self) -> float:
        """Aggregate suppression gain."""
        return self.total_raw_candidates / max(self.total_targets, 1)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the report."""
        return {
            "stratum": self.name,
            "n_scenes": self.n_scenes,
            "area_km2": round(self.total_area_km2, 1),
            "targets": self.total_targets,
            "raw_candidates": self.total_raw_candidates,
            "density_per_1000km2": round(self.density_per_1000km2, 3),
            "raw_density_per_1000km2": round(self.raw_density_per_1000km2, 3),
            "suppression_factor": round(self.suppression_factor, 2),
        }


def summarise_by(
    results: Sequence[SceneDetectionResult],
    key: str,
) -> list[StratumSummary]:
    """Aggregate results into strata by a named attribute of the result."""
    buckets: dict[str, list[SceneDetectionResult]] = defaultdict(list)
    for r in results:
        buckets[str(getattr(r, key))].append(r)

    summaries: list[StratumSummary] = []
    for name, group in sorted(buckets.items()):
        summaries.append(
            StratumSummary(
                name=name,
                n_scenes=len(group),
                total_area_km2=sum(r.analysed_area_km2 for r in group),
                total_targets=sum(len(r.targets) for r in group),
                total_raw_candidates=sum(r.raw_candidates for r in group),
            )
        )
    return summaries


def aggregate_suppression(results: Sequence[SceneDetectionResult]) -> list[dict[str, Any]]:
    """Sum the suppression ledger across scenes, preserving stage order."""
    order: list[str] = []
    removed: dict[str, int] = defaultdict(int)
    for r in results:
        for stage, n_removed, _ in r.suppression.stages:
            if stage not in removed:
                order.append(stage)
            removed[stage] += n_removed

    total_raw = sum(r.raw_candidates for r in results)
    rows: list[dict[str, Any]] = []
    running = total_raw
    for stage in order:
        running -= removed[stage]
        rows.append(
            {
                "stage": stage,
                "removed": removed[stage],
                "remaining": running,
                "pct_of_raw": round(100.0 * removed[stage] / max(total_raw, 1), 2),
            }
        )
    return rows


class DetectionBenchmark:
    """Runs a detector configuration across a scene set and reports operating points."""

    def __init__(
        self,
        output_dir: Path | str = "./data/processed/benchmarks",
        suppression: SuppressionConfig | None = None,
    ) -> None:
        """Create a benchmark writing artefacts under ``output_dir``."""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.suppression = suppression or SuppressionConfig()
        self.run_manifests: list[dict[str, Any]] = []

    def run_scene_set(
        self,
        extents: Sequence[SceneExtent],
        detector_kind: str = "gamma",
        pfa: float = 1e-5,
        limit: int | None = None,
    ) -> list[SceneDetectionResult]:
        """Run one detector configuration over a set of scenes."""
        runner = SceneDetectionRunner(
            detector_kind=detector_kind, pfa=pfa, suppression=self.suppression
        )
        if limit is not None and limit < 1:
            raise ValueError("limit must be a positive integer")
        chosen = list(extents)[:limit] if limit is not None else list(extents)
        results: list[SceneDetectionResult] = []
        manifest: dict[str, Any] = {
            "detector": detector_kind,
            "pfa": pfa,
            "eligible_scenes": len(extents),
            "selected_scenes": len(chosen),
            "processed_scenes": 0,
            "failed_scenes": [],
            "skipped_scenes": [],
        }
        self.run_manifests.append(manifest)

        for i, extent in enumerate(chosen, start=1):
            try:
                scene = load_scene(extent.path)
            except Exception as exc:  # noqa: BLE001 - archive integrity varies
                manifest["failed_scenes"].append({"scene_id": extent.scene_id, "error": str(exc)})
                logger.warning("Skipping %s: %s", extent.scene_id, exc)
                continue
            logger.info("[%d/%d] %s (%s)", i, len(chosen), extent.scene_id, detector_kind)
            result = runner.run(scene)
            if result.analysed_area_km2 <= 0:
                manifest["skipped_scenes"].append(
                    {
                        "scene_id": extent.scene_id,
                        "reason": "No eligible water after AOI, quality and training-support masks",
                        "mask_breakdown": result.mask_breakdown,
                    }
                )
                continue
            with extent.path.open("rb") as source:
                result.assumptions["source_sha256"] = hashlib.file_digest(
                    source, "sha256"
                ).hexdigest()
            results.append(result)
            manifest["processed_scenes"] += 1

        # Wind regimes are relative terciles across the cohort, so they can only
        # be assigned once every scene in the run has been measured.
        assign_wind_regimes(results)
        (self.output_dir / "benchmark_run_manifest.json").write_text(
            json.dumps(self.run_manifests, indent=2, allow_nan=False), encoding="utf-8"
        )
        return results

    def sweep_pfa(
        self,
        extents: Sequence[SceneExtent],
        pfa_values: Sequence[float],
        detector_kind: str = "gamma",
        limit: int | None = None,
    ) -> dict[float, list[SceneDetectionResult]]:
        """Run the detector across several false-alarm probabilities.

        Produces the operating-point curve that lets a reader see the cost of
        the chosen Pfa rather than taking a single tuned number on trust.
        """
        sweep: dict[float, list[SceneDetectionResult]] = {}
        for pfa in pfa_values:
            logger.info("--- Pfa sweep: %.1e ---", pfa)
            sweep[pfa] = self.run_scene_set(extents, detector_kind, pfa, limit)
        return sweep

    def write_report(
        self,
        results: Sequence[SceneDetectionResult],
        sweep: dict[float, list[SceneDetectionResult]] | None = None,
        detector_label: str = "Gamma-CFAR",
    ) -> dict[str, Any]:
        """Write JSON results, a markdown table, and the operating-point plot."""
        if not results or sum(r.analysed_area_km2 for r in results) <= 0:
            raise ValueError("Cannot report a benchmark without eligible analysed water")
        by_ice = summarise_by(results, "ice_regime")
        by_wind = summarise_by(results, "wind_regime")
        overall = StratumSummary(
            name="all",
            n_scenes=len(results),
            total_area_km2=sum(r.analysed_area_km2 for r in results),
            total_targets=sum(len(r.targets) for r in results),
            total_raw_candidates=sum(r.raw_candidates for r in results),
        )

        report: dict[str, Any] = {
            "schema_version": 2,
            "methodology": {
                "metric": "unverified_candidate_density_per_1000km2",
                "precision_recall_measured": False,
                "design_pfa_is_measured_far": False,
                "classification": "unclassified; radiometric heuristic stored separately",
                "confidence_kind": "uncalibrated_heuristic_score",
                "aoi": "configs/aoi.geojson newfoundland_labrador_marine; per-pixel centre clipping",
                "area": "eligible pixel count times nominal spacing squared; exposure sum, not unique ocean area",
                "suppression": asdict(self.suppression),
                "source_scaling": "See per-scene assumptions and source_sha256",
            },
            "run_manifests": self.run_manifests,
            "detector": detector_label,
            "n_scenes": len(results),
            "overall": overall.to_dict(),
            "by_ice_regime": [s.to_dict() for s in by_ice],
            "by_wind_regime": [s.to_dict() for s in by_wind],
            "suppression_ledger": aggregate_suppression(results),
            "scenes": [r.to_dict() for r in results],
        }

        if sweep:
            report["pfa_sweep"] = [
                {
                    "pfa": pfa,
                    "n_scenes": len(rs),
                    "area_km2": round(sum(r.analysed_area_km2 for r in rs), 1),
                    "targets": sum(len(r.targets) for r in rs),
                    "density_per_1000km2": round(
                        1000.0
                        * sum(len(r.targets) for r in rs)
                        / max(sum(r.analysed_area_km2 for r in rs), 1e-9),
                        3,
                    ),
                    "raw_density_per_1000km2": round(
                        1000.0
                        * sum(r.raw_candidates for r in rs)
                        / max(sum(r.analysed_area_km2 for r in rs), 1e-9),
                        3,
                    ),
                }
                for pfa, rs in sorted(sweep.items())
            ]

        json_path = self.output_dir / "benchmark_results.json"
        json_path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        logger.info("Wrote %s", json_path)

        md_path = self.output_dir / "benchmark_table.md"
        md_path.write_text(self._render_markdown(report), encoding="utf-8")
        logger.info("Wrote %s", md_path)

        if sweep:
            self._plot_operating_points(report)

        return report

    @staticmethod
    def _render_markdown(report: dict[str, Any]) -> str:
        """Render the benchmark as a markdown document."""
        lines: list[str] = []
        o = report["overall"]
        lines.append(f"# Detection benchmark — {report['detector']}")
        lines.append("")
        lines.append(
            "Unverified candidate density only. Precision, recall and operational false-alarm rate have not been measured. Area sums acquisition exposures and is not unique water coverage."
        )
        lines.append("")
        lines.append(
            f"{o['n_scenes']} Sentinel-1 EW scenes, {o['area_km2']:,.0f} km² of analysed water."
        )
        lines.append("")
        lines.append("## Overall")
        lines.append("")
        lines.append("| metric | value |")
        lines.append("|---|---|")
        lines.append(f"| Detections per 1000 km² | **{o['density_per_1000km2']:.2f}** |")
        lines.append(f"| Raw CFAR candidates per 1000 km² | {o['raw_density_per_1000km2']:.2f} |")
        lines.append(f"| Suppression factor | {o['suppression_factor']:.1f}× |")
        lines.append(f"| Total detections | {o['targets']:,} |")
        lines.append("")

        for title, key in (
            ("By sea ice regime", "by_ice_regime"),
            ("By relative wind regime", "by_wind_regime"),
        ):
            lines.append(f"## {title}")
            lines.append("")
            lines.append(
                "| stratum | scenes | area km² | detections | per 1000 km² | suppression |"
            )
            lines.append("|---|---|---|---|---|---|")
            for s in report[key]:
                lines.append(
                    f"| {s['stratum']} | {s['n_scenes']} | {s['area_km2']:,.0f} | "
                    f"{s['targets']:,} | {s['density_per_1000km2']:.2f} | "
                    f"{s['suppression_factor']:.1f}× |"
                )
            lines.append("")

        lines.append("## Suppression ledger")
        lines.append("")
        lines.append("| stage | removed | remaining | % of raw |")
        lines.append("|---|---|---|---|")
        for row in report["suppression_ledger"]:
            lines.append(
                f"| {row['stage']} | {row['removed']:,} | {row['remaining']:,} | {row['pct_of_raw']:.1f}% |"
            )
        lines.append("")

        if "pfa_sweep" in report:
            lines.append("## Operating points")
            lines.append("")
            lines.append("| Pfa | raw per 1000 km² | final per 1000 km² |")
            lines.append("|---|---|---|")
            for row in report["pfa_sweep"]:
                lines.append(
                    f"| {row['pfa']:.0e} | {row['raw_density_per_1000km2']:.2f} | "
                    f"{row['density_per_1000km2']:.2f} |"
                )
            lines.append("")

        return "\n".join(lines)

    def _plot_operating_points(self, report: dict[str, Any]) -> None:
        """Plot detection density against Pfa, before and after suppression."""
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        rows = report.get("pfa_sweep", [])
        if not rows:
            return

        pfa = [r["pfa"] for r in rows]
        raw = [r["raw_density_per_1000km2"] for r in rows]
        final = [r["density_per_1000km2"] for r in rows]

        fig, ax = plt.subplots(figsize=(7.0, 4.5))
        ax.plot(pfa, raw, "o--", label="Raw CFAR candidates", color="#b0413e")
        ax.plot(pfa, final, "o-", label="After suppression chain", color="#1f4e79")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("CFAR design $P_{fa}$")
        ax.set_ylabel("Detections per 1000 km²")
        ax.set_title(
            f"{report['detector']} operating points\n"
            f"Newfoundland & Labrador shelf, {report['overall']['n_scenes']} S1 EW scenes"
        )
        ax.grid(True, which="both", alpha=0.3)
        ax.legend()
        fig.tight_layout()

        out = self.output_dir / "operating_points.png"
        fig.savefig(out, dpi=150)
        plt.close(fig)
        logger.info("Wrote %s", out)
