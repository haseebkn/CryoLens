"""Compare annotated orbit vectors with official precise EOFs; no correction applied."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicHermiteSpline

from cryolens.eval.cohort import file_digest
from cryolens.preprocess.safe_reader import SAFEProductReader, _open_path


def utc(text: str | None) -> datetime:
    if text is None:
        raise ValueError("Missing UTC orbit timestamp")
    value = datetime.fromisoformat(text.removeprefix("UTC="))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def required_text(node: ET.Element, tag: str) -> str:
    value = node.findtext(tag)
    if value is None:
        raise ValueError("Missing orbit field: " + tag)
    return value


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--raw-root", type=Path, required=True)
    p.add_argument("--orbit-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError("Use a fresh orbit consistency receipt")
    plan = json.loads(args.plan.read_text())
    evidence = json.loads((args.orbit_root / "download-receipt.json").read_text())
    records = []
    for receipt in evidence["products"]:
        if not receipt["official_orbit_available"]:
            continue
        item = next(r for r in plan["selected"] if r["name"] == receipt["product"])
        path = args.orbit_root / "official-eof" / receipt["orbit"]
        if file_digest(path) != receipt["sha256"]:
            raise ValueError("Official auxiliary orbit bytes changed")
        root = ET.parse(path).getroot()
        osv = root.findall(".//OSV")
        epoch = utc(osv[0].findtext("UTC"))
        times = np.array([(utc(v.findtext("UTC")) - epoch).total_seconds() for v in osv])
        positions = np.array(
            [[float(required_text(v, axis)) for axis in ("X", "Y", "Z")] for v in osv]
        )
        velocities = np.array(
            [[float(required_text(v, axis)) for axis in ("VX", "VY", "VZ")] for v in osv]
        )
        reference = CubicHermiteSpline(times, positions, velocities, extrapolate=False)
        safe = args.raw_root / item["name"].removesuffix(".SAFE") / item["name"]
        reader = SAFEProductReader(safe)
        channels = {}
        for pol in ("HH", "HV"):
            annotation = Path(_open_path(reader._find_for_polarisation(pol)[1]))
            before = file_digest(annotation)
            annotated = ET.parse(annotation).getroot().findall(".//orbitList/orbit")
            if any(v.findtext("frame") != "Earth Fixed" for v in annotated):
                raise ValueError("Orbit frames differ")
            sampled_times = np.array(
                [(utc(v.findtext("time")) - epoch).total_seconds() for v in annotated]
            )
            actual_positions = np.array(
                [[float(required_text(v, "position/" + a)) for a in "xyz"] for v in annotated]
            )
            actual_velocities = np.array(
                [[float(required_text(v, "velocity/" + a)) for a in "xyz"] for v in annotated]
            )
            position_error = np.linalg.norm(actual_positions - reference(sampled_times), axis=1)
            velocity_error = np.linalg.norm(actual_velocities - reference(sampled_times, 1), axis=1)
            if not np.isfinite(position_error).all() or not np.isfinite(velocity_error).all():
                raise ValueError("Official orbit does not cover every annotated vector")
            if file_digest(annotation) != before:
                raise ValueError("Annotation changed during orbit comparison")
            channels[pol] = {
                "annotation_sha256": before,
                "annotated_vectors": len(annotated),
                "position_difference_m_percentiles": np.percentile(
                    position_error, [50, 95, 100]
                ).tolist(),
                "velocity_difference_m_s_percentiles": np.percentile(
                    velocity_error, [50, 95, 100]
                ).tolist(),
            }
        records.append(
            {
                "product": item["name"],
                "official_eof": receipt["orbit"],
                "official_eof_sha256": receipt["sha256"],
                "channels": channels,
            }
        )
        print(
            item["name"],
            "position difference m",
            channels["HH"]["position_difference_m_percentiles"],
            flush=True,
        )
    report = {
        "schema_version": 1,
        "driver_sha256": file_digest(Path(__file__)),
        "products": records,
        "orbit_correction_applied": False,
        "claim_boundary": "Consistency of product ephemerides with separately published official precise orbit vectors. No orbit substitution, ground-control validation, absolute field accuracy or scientific release.",
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)


if __name__ == "__main__":
    main()
