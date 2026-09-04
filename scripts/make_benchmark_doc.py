"""Render docs/BENCHMARK.md from a benchmark run's JSON output.

Kept as a script rather than folded into the harness so the published document
is regenerated deliberately, and so the numbers in the repository always
correspond to a run someone chose to publish.

Usage:
    python scripts/make_benchmark_doc.py [results.json] [--out docs/BENCHMARK.md]
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

DEFAULT_RESULTS = Path("data/processed/benchmarks/benchmark_results.json")
DEFAULT_OUT = Path("docs/BENCHMARK.md")
PLOT_SOURCE = Path("data/processed/benchmarks/operating_points.png")
PLOT_DEST = Path("docs/benchmarks/operating_points.png")


def _stratum_table(rows: list[dict[str, Any]], label: str) -> list[str]:
    """Render one stratified table."""
    out = [
        f"### {label}",
        "",
        "| stratum | scenes | water area (km²) | detections | per 1000 km² | suppression |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['stratum']} | {r['n_scenes']} | {r['area_km2']:,.0f} | "
            f"{r['targets']:,} | {r['density_per_1000km2']:.2f} | "
            f"{r['suppression_factor']:.1f}× |"
        )
    out.append("")
    return out


def render(report: dict[str, Any], has_plot: bool) -> str:
    """Build the full markdown document."""
    o = report["overall"]
    lines: list[str] = []

    lines += [
        "# Detection Benchmark",
        "",
        f"**Detector:** {report['detector']} · **Region:** Newfoundland & Labrador shelf  ",
        f"**Scenes:** {o['n_scenes']} Sentinel-1 EW GRDM acquisitions · "
        f"**Analysed water:** {o['area_km2']:,.0f} km²",
        "",
        "---",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Raw CFAR candidates per 1000 km² | {o['raw_density_per_1000km2']:.2f} |",
        f"| **After suppression, per 1000 km²** | **{o['density_per_1000km2']:.2f}** |",
        f"| Suppression factor | {o['suppression_factor']:.1f}× |",
        f"| Total detections retained | {o['targets']:,} |",
        f"| Total raw candidates | {o['raw_candidates']:,} |",
        "",
        "### What this number is",
        "",
        "**Detection density per 1000 km² of analysed water.** Over open water away",
        "from land and ice, genuine icebergs are sparse at Sentinel-1 EW resolution,",
        "so this figure is dominated by false alarms and is reported as an **upper",
        "bound on the false-alarm rate**.",
        "",
        "It is **not** precision, recall, or mAP. No verified iceberg positions exist",
        "for these scenes — see [LIMITATIONS.md](LIMITATIONS.md) §1. Every rate is",
        "normalised by the water area actually examined after masking, because false",
        "alarms *per scene* is meaningless when swath coverage and land fraction differ",
        "between acquisitions.",
        "",
        "---",
        "",
        "## Stratified results",
        "",
    ]

    lines += _stratum_table(report["by_ice_regime"], "By sea ice regime")
    lines += [
        "`unknown` covers the AI4Arctic challenge test scenes, whose ice charts are",
        "withheld. They are reported separately rather than folded into open water,",
        "which would understate detection density over ice.",
        "",
    ]
    lines += _stratum_table(report["by_wind_regime"], "By relative wind regime")
    lines += [
        "Wind bins are **terciles across this scene cohort**, not absolute m/s, and",
        "not within-scene quantiles. The ERA5 fields in this distribution are",
        "standardised with no recorded extremes, so metres per second is",
        "unrecoverable (LIMITATIONS §3); the per-scene median still orders scenes",
        "correctly, so the split is between scenes rather than inside one.",
        "`unknown` marks scenes carrying no wind field, which are excluded from the",
        "tercile fit.",
        "",
        "---",
        "",
        "## Suppression ledger",
        "",
        "Where the candidate budget went, summed across every scene:",
        "",
        "| stage | removed | remaining | % of raw |",
        "|---|---:|---:|---:|",
    ]
    for row in report["suppression_ledger"]:
        lines.append(
            f"| `{row['stage']}` | {row['removed']:,} | {row['remaining']:,} | "
            f"{row['pct_of_raw']:.1f}% |"
        )

    lines += [
        "",
        "This ledger is published rather than summarised because it shows something a",
        "single aggregate figure would hide: **one stage does most of the work.**",
        "CFAR at this Pfa produces predominantly isolated single-pixel speckle hits,",
        "while genuine targets form multi-pixel clusters, so the minimum-size gate",
        "carries the bulk of the suppression.",
        "",
        "The recall cost of that threshold is **not measured**. Raising it removes",
        "false alarms and small icebergs together, and without ground truth the",
        "trade-off cannot be located (LIMITATIONS §9).",
        "",
    ]

    if "pfa_sweep" in report:
        lines += [
            "---",
            "",
            "## Operating points",
            "",
            "Detection density against the CFAR design false-alarm probability, before",
            "and after the suppression chain. Published so the chosen operating point",
            "can be read off a curve rather than taken on trust.",
            "",
            "| Pfa | raw per 1000 km² | after suppression | scenes |",
            "|---|---:|---:|---:|",
        ]
        for row in report["pfa_sweep"]:
            lines.append(
                f"| {row['pfa']:.0e} | {row['raw_density_per_1000km2']:.2f} | "
                f"{row['density_per_1000km2']:.2f} | {row['n_scenes']} |"
            )
        lines.append("")
        sweep_scenes = {row["n_scenes"] for row in report["pfa_sweep"]}
        headline_scenes = report["overall"]["n_scenes"]
        if sweep_scenes and sweep_scenes != {headline_scenes}:
            n = sorted(sweep_scenes)[0]
            lines += [
                f"The sweep runs on a {n}-scene subset rather than the full "
                f"{headline_scenes}, because each additional Pfa multiplies the "
                "compute by a whole pass over the cohort. Absolute densities here "
                "are therefore **not** directly comparable with the headline "
                "figure above; the curve's shape and the ratio between operating "
                "points are what it is published for.",
                "",
            ]
        if has_plot:
            lines += ["![Operating points](benchmarks/operating_points.png)", ""]

    comparison_path = Path("data/processed/benchmarks/detector_comparison.json")
    if comparison_path.is_file():
        comp = json.loads(comparison_path.read_text(encoding="utf-8"))
        any_row = next(iter(comp.values()))
        lines += [
            "---",
            "",
            "## Two detectors, one harness",
            "",
            f"Cell-averaging and Gamma/K-distribution CFAR run over the same "
            f"{any_row['n_scenes']}-scene subset ({any_row['area_km2']:,.0f} km²), same "
            "suppression chain, same Pfa:",
            "",
            "| Detector | raw per 1000 km² | after suppression | detections | suppression |",
            "|---|---:|---:|---:|---:|",
        ]
        for label, v in sorted(comp.items()):
            lines.append(
                f"| {label} | {v['raw_density_per_1000km2']:.2f} | "
                f"{v['density_per_1000km2']:.3f} | {v['targets']:,} | "
                f"{v['suppression_factor']:.1f}× |"
            )
        lines += [
            "",
            "**This table does not say which detector is better, and it cannot.**",
            "",
            "CA-CFAR retains far fewer targets. That is the expected consequence of",
            "its clutter model: the cell-averaging estimator sets its threshold from",
            "the local *mean*, and over sea ice the mean is inflated by the same",
            "heavy tail the detector is trying to separate from, so the threshold",
            "rises and genuine targets fall below it. The Gamma detector estimates a",
            "shape parameter by method of moments and adapts to that tail, which is",
            "precisely why ADR-008 specifies it for sea states at or above 3.",
            "",
            "Whether CA-CFAR's lower density represents *fewer false alarms* or",
            "*fewer detections of real targets* is not determinable from these",
            "numbers. Separating the two requires verified positives, which do not",
            "exist for these scenes (LIMITATIONS §1). Reporting the comparison as a",
            "win for either detector would be reading a result the data does not",
            "support.",
            "",
        ]

    lines += [
        "---",
        "",
        "## Reproducing",
        "",
        "```bash",
        "make fetch-shorelines   # GSHHG coastlines for land masking",
        "make scene-index        # index the AI4Arctic archive by extent",
        "make benchmark SWEEP=1  # run and write artefacts",
        "python scripts/make_benchmark_doc.py",
        "```",
        "",
        "Scene selection requires the scene **centre** inside the AOI",
        "(64.5°W–44.0°W, 42.5°N–60.5°N), not merely an overlap: an EW swath is about",
        "400 km across and can clip the corner of the box while lying almost entirely",
        "outside the region.",
        "",
        "## Per-scene results",
        "",
        "| scene | ice regime | wind | water km² | raw | kept | per 1000 km² |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for s in report["scenes"]:
        lines.append(
            f"| `{s['scene_id'].replace('_prep.nc', '')}` | {s['ice_regime']} | "
            f"{s['wind_regime']} | {s['analysed_area_km2']:,.0f} | "
            f"{s['raw_candidates']:,} | {s['n_targets']:,} | "
            f"{s['detections_per_1000km2']:.2f} |"
        )
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    """Render the document and copy the plot into docs/."""
    p = argparse.ArgumentParser()
    p.add_argument("results", nargs="?", default=str(DEFAULT_RESULTS))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()

    results_path = Path(args.results)
    if not results_path.is_file():
        raise SystemExit(f"No benchmark results at {results_path}. Run `make benchmark` first.")

    report = json.loads(results_path.read_text(encoding="utf-8"))

    has_plot = PLOT_SOURCE.is_file()
    if has_plot:
        PLOT_DEST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLOT_SOURCE, PLOT_DEST)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report, has_plot), encoding="utf-8")
    print(f"Wrote {out}")
    if has_plot:
        print(f"Copied plot to {PLOT_DEST}")

    o = report["overall"]
    print(f"  scenes {o['n_scenes']}, {o['area_km2']:,.0f} km2")
    print(
        f"  raw {o['raw_density_per_1000km2']:.2f} -> final {o['density_per_1000km2']:.2f} per 1000 km2"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
