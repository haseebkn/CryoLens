"""Operational benchmark harness for maritime SAR target detection.

What this measures, and what it does not
----------------------------------------
The AI4Arctic distribution carries ice charts, not iceberg point truth, and no
freely available dataset provides verified iceberg positions co-registered to
these acquisitions. This harness therefore reports the metric that *can* be
measured rigorously without label leakage:

    **detection density per 1000 square kilometres of analysed water**

stratified by sea ice regime and relative wind regime, together with the
suppression ledger showing where the candidate budget went.

Over open water away from land and ice, at the resolution of Sentinel-1 EW,
genuine icebergs are sparse. Detection density in that stratum is therefore
dominated by false alarms and functions as a defensible upper bound on the
false-alarm rate. It is reported as such, and never labelled precision or
recall, because no verified positives exist to support those terms.

Recall against IIP sightings requires the NASA Earthdata credential path and is
implemented separately in :mod:`cryolens.eval.correlate`; see docs/LIMITATIONS.md
for why IIP cannot be intersected naively with SAR pixels.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryolens.data.ai4arctic import SceneExtent, load_scene
from cryolens.detect.filters import SuppressionConfig
from cryolens.detect.runner import SceneDetectionResult, SceneDetectionRunner

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
        chosen = list(extents)[:limit] if limit else list(extents)
        results: list[SceneDetectionResult] = []

        for i, extent in enumerate(chosen, start=1):
            try:
                scene = load_scene(extent.path)
            except Exception as exc:  # noqa: BLE001 - archive integrity varies
                logger.warning("Skipping %s: %s", extent.scene_id, exc)
                continue
            logger.info("[%d/%d] %s (%s)", i, len(chosen), extent.scene_id, detector_kind)
            results.append(runner.run(scene))

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
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
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
            lines.append("| stratum | scenes | area km² | detections | per 1000 km² | suppression |")
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
