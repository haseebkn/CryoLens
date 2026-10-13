"""Additive re-screening of existing optical pairs without changing source receipts."""

import argparse
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryolens.eval.cohort import file_digest
from cryolens.review.evidence import screened_visibility
from cryolens.review.pairs import verify_pair_files


def rescreen(root: Path, output: Path) -> dict[str, Any]:
    report_path = root / "report.json"
    report_hash = file_digest(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    checkout = Path(__file__).parents[3]
    snapshot = output / "executed-code.zip"
    with zipfile.ZipFile(snapshot, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((checkout / "src/cryolens").rglob("*.py")):
            archive.write(path, path.relative_to(checkout).as_posix())
        for name in ("pyproject.toml", "uv.lock"):
            archive.write(checkout / name, name)
        for path in sorted((checkout / "configs").rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(checkout).as_posix())
    result: dict[str, Any] = {
        "schema_version": 1,
        "execution_kind": "strict_buffer_rescreen_v1",
        "executed_at": datetime.now(UTC).isoformat(),
        "source_root": str(root),
        "source_report_sha256": report_hash,
        "executed_code_sha256": file_digest(snapshot),
        "scope": "Existing aligned pixels only; no new queries, downloads, annotations or source mutations",
        "denominator": report["denominator"],
        "locations": [],
    }
    for location in report["locations"]:
        record: dict[str, Any] = {"id": location["id"], "mode": location["mode"], "pairs": []}
        for reference in location["pairs"]:
            folder = root / "pairs" / reference["id"]
            manifest_path = folder / "pair.json"
            if file_digest(manifest_path) != reference["manifest_sha256"]:
                raise ValueError("Original pair manifest differs from report")
            pair = json.loads(manifest_path.read_text(encoding="utf-8"))
            verify_pair_files(folder, pair)
            visibility = (
                screened_visibility(folder / "optical-aligned.tif", pair)
                if pair["optical_item_id"]
                else pair["visibility"]
            )
            record["pairs"].append(
                {
                    "id": pair["id"],
                    "manifest_sha256": reference["manifest_sha256"],
                    "optical_item_id": pair["optical_item_id"],
                    "original_visibility": pair["visibility"],
                    "strict_visibility": visibility,
                    "full_movement_envelope_in_chip": pair["full_movement_envelope_in_chip"],
                }
            )
            verify_pair_files(folder, pair)
            if file_digest(manifest_path) != reference["manifest_sha256"]:
                raise ValueError("Source manifest changed during screening")
        result["locations"].append(record)
    if file_digest(report_path) != report_hash:
        raise ValueError("Source report changed during screening")
    result["counts"] = {}
    for mode in ("EW", "IW", "combined"):
        locations = [r for r in result["locations"] if mode == "combined" or r["mode"] == mode]
        result["counts"][mode] = {
            "locations": len(locations),
            "optical_context": sum(
                any(p["optical_item_id"] for p in r["pairs"]) for r in locations
            ),
            "strict_useful": sum(
                any(p["strict_visibility"]["useful_for_review"] for p in r["pairs"])
                for r in locations
            ),
            "strict_full_movement": sum(
                any(
                    p["strict_visibility"]["useful_for_review"]
                    and p["full_movement_envelope_in_chip"]
                    for p in r["pairs"]
                )
                for r in locations
            ),
        }
    (output / "receipt.json").write_text(
        json.dumps(result, indent=2, allow_nan=False), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = rescreen(args.root, args.output)
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
