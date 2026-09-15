"""Render an audited candidate-density report, rejecting legacy headline inputs."""

import argparse
import json
from pathlib import Path
from typing import Any

# Minimum acquisitions before a stratified row is treated as interpretable.
# Below this a row is a single observation: the published three-scene run split
# its wind terciles one scene per bin, which produced suppression factors
# ranging from 11x to 582x that describe individual scenes, not regimes.
MIN_SCENES_PER_STRATUM = 3


def render(report: dict[str, Any], has_plot: bool = False) -> str:
    """Publish only versioned reports with explicit methodology and run accounting."""
    if (
        report.get("schema_version") != 2
        or not report.get("methodology")
        or not report.get("run_manifests")
    ):
        raise ValueError("Legacy/unversioned reports cannot be published as audited evidence")
    overall = report["overall"]
    if overall["area_km2"] <= 0 or overall["n_scenes"] <= 0:
        raise ValueError("A benchmark needs positive analyzed coverage")
    lines = [
        "# Audited candidate-density benchmark",
        "",
        "**Research result. Precision, recall and empirical false-positive rate are not measured.**",
        "",
        "This report supersedes the withdrawn historical 39-scene headline. "
        "Its counts cannot be compared as detector improvements: the normalization, "
        "CFAR model, geographic mask and area denominator changed.",
        "",
        f"Detector: **{report['detector']}**. Region: **NL shelf study polygon**.",
        "",
        "| Quantity | Value |",
        "|---|---:|",
        f"| Processed scenes | {overall['n_scenes']} |",
        f"| Cumulative analyzed area (km², nominal spacing) | {overall['area_km2']:,.1f} |",
        f"| Raw connected candidates | {overall['raw_candidates']:,} |",
        f"| Retained unverified candidates | {overall['targets']:,} |",
        f"| Raw candidates / 1,000 km² | {overall['raw_density_per_1000km2']:.3f} |",
        f"| Retained candidates / 1,000 km² | {overall['density_per_1000km2']:.3f} |",
        "",
        "Candidate density counts all retained returns regardless of their true identity. "
        "Fewer returns can include missed real targets. Repeated observations accumulate "
        "coverage; this is not unique ocean area. Masked water was not surveyed by the detector.",
        "",
        "## Methodology",
        "",
        "```json",
        json.dumps(report["methodology"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Run accounting",
        "",
        "```json",
        json.dumps(report["run_manifests"], indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    for key, label in [("by_ice_regime", "Sea-ice strata"), ("by_wind_regime", "Wind strata")]:
        rows = report.get(key, [])
        if not rows:
            continue
        lines.extend(
            [
                f"## {label}",
                "",
                "| Stratum | Scenes | Area km² | Candidates | / 1,000 km² | Interpretable |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
        underpowered = []
        for row in rows:
            adequate = row["n_scenes"] >= MIN_SCENES_PER_STRATUM
            if not adequate:
                underpowered.append(row["stratum"])
            marker = "yes" if adequate else f"**no — n<{MIN_SCENES_PER_STRATUM}**"
            lines.append(
                f"| {row['stratum']} | {row['n_scenes']} | {row['area_km2']:,.1f} | "
                f"{row['targets']} | {row['density_per_1000km2']:.3f} | {marker} |"
            )
        lines.append("")
        if underpowered:
            lines.extend(
                [
                    f"Strata marked **no** ({', '.join(underpowered)}) contain fewer than "
                    f"{MIN_SCENES_PER_STRATUM} acquisitions. Their densities and suppression "
                    "factors are single-scene observations, not estimates of a regime, and a "
                    "difference between them is not evidence of a trend. They are shown for "
                    "completeness of accounting rather than for comparison.",
                    "",
                ]
            )
    ledger = report.get("suppression_ledger", [])
    if ledger:
        lines.extend(
            ["## Suppression ledger", "", "| Stage | Removed | Remaining |", "|---|---:|---:|"]
        )
        for row in ledger:
            lines.append(f"| {row['stage']} | {row['removed']:,} | {row['remaining']:,} |")
        lines.append("")
    lines.extend(
        [
            "## Reproduction and evidence",
            "",
            "Machine-readable evidence with per-scene assumptions, source checksums "
            "and suppression ledgers: [audited_results.json](benchmarks/audited_results.json). "
            "See [DATA.md](DATA.md), [AUDIT.md](AUDIT.md) and [LIMITATIONS.md](LIMITATIONS.md).",
            "",
            "No certified C-CORE operating point, MANICE confidence code or navigational "
            "hazard assessment is established by this experiment.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--out", type=Path, default=Path("docs/BENCHMARK.md"))
    args = parser.parse_args()
    report = json.loads(args.results.read_text(encoding="utf-8"))
    output = render(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
