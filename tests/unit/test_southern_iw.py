"""Exercise southern catalogue pagination and signed noise diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from validation import search_southern_iw
from validation.diagnose_iw_noise import summarize


def test_noise_diagnostic_retains_nonpositive_observations() -> None:
    raw = np.array([[1.0, 2.0, 4.0, np.nan]], dtype=np.float32)
    corrected = np.array([[-1.0, 0.0, 2.0, np.nan]], dtype=np.float32)
    result = summarize(raw, corrected)
    assert result["finite_pixels"] == 3
    assert result["nonpositive_fraction"] == pytest.approx(2 / 3)
    assert result["mean_signed_corrected_power"] == pytest.approx(1 / 3)
    assert result["mean_annotated_noise_power"] == 2.0
    assert result["raw_to_annotated_noise_ratio_percentiles"][1] == 1.0
    assert np.array_equal(corrected, [[-1.0, 0.0, 2.0, np.nan]], equal_nan=True)


def test_noise_diagnostic_rejects_incompatible_grids_and_negative_noise() -> None:
    with pytest.raises(ValueError, match="grids"):
        summarize(np.ones((2, 2)), np.ones((3, 2)))
    with pytest.raises(ValueError, match="negative"):
        summarize(np.array([[1.0]]), np.array([[2.0]]))
    result = summarize(np.full((1, 1), np.nan), np.full((1, 1), np.nan))
    assert result["finite_pixels"] == 0
    assert result["nonpositive_fraction"] is None


def catalogue_item(identifier: str, date: str) -> dict[str, Any]:
    return {
        "Id": identifier,
        "Name": f"S1A_IW_GRDH_1SDH_{date[:10].replace('-', '')}T090000_20260102T090025_000001_000001_ABCD_COG.SAFE",
        "ContentDate": {"Start": date},
        "ContentLength": 1000000,
        "GeoFootprint": {
            "type": "Polygon",
            "coordinates": [[[-52, 45], [-51, 45], [-51, 46], [-52, 46], [-52, 45]]],
        },
        "Attributes": [{"Name": "polarisationChannels", "Value": "HH&HV"}],
    }


def test_search_consumes_all_pages_and_excludes_test_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [catalogue_item("first", "2026-01-02T09:00:00Z")],
                    "@odata.nextLink": search_southern_iw.ENDPOINT + "?page=2",
                },
            )
        return httpx.Response(
            200, json={"value": [catalogue_item("second", "2026-01-16T09:00:00Z")]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(search_southern_iw.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(
        search_southern_iw,
        "load_manifest",
        lambda path: {
            "scenes": [
                {
                    "partition": "test",
                    "acquired_utc": "2026-01-16T09:00:00Z",
                    "acquisition_id": "held-out",
                }
            ]
        },
    )
    result = search_southern_iw.search("2026-01-01", "2026-02-01")
    assert len(calls) == 2
    assert result["complete_pagination"] is True
    assert [row["id"] for row in result["candidates"]] == ["first"]
    assert result["excluded"][0]["id"] == "second"
    assert result["candidates"][0]["southern_fraction"] == pytest.approx(1.0)


def test_search_refuses_foreign_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"value": [], "@odata.nextLink": "https://example.org/foreign"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(search_southern_iw.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(search_southern_iw, "load_manifest", lambda path: {"scenes": []})
    with pytest.raises(ValueError, match="pagination"):
        search_southern_iw.search("2026-01-01", "2026-02-01")


def test_publication_plan_scope_and_conflicting_identity(tmp_path: Path) -> None:
    import json

    from validation.publish_acquisition_search import selected_records

    row = {"name": "selected.SAFE", "id": "first"}
    first = tmp_path / "explicit-plan.json"
    second = tmp_path / "unrelated-plan.json"
    first.write_text(json.dumps({"selected": [row]}))
    second.write_text(json.dumps({"selected": [{"name": "other.SAFE", "id": "other"}]}))
    assert list(selected_records([first])) == ["selected.SAFE"]
    assert len(selected_records([first, second])) == 2
    second.write_text(json.dumps({"selected": [{"name": "selected.SAFE", "id": "changed"}]}))
    with pytest.raises(ValueError, match="Conflicting catalogue"):
        selected_records([first, second])


def test_weather_context_is_temporally_bound_and_uses_sea_grid() -> None:
    from validation.screen_southern_wind import weather_parameters, wind_context

    row = {"date": "2025-12-06T09:33:05Z", "overlap_centroid": [-51.23735, 46.21996]}
    options = weather_parameters([row, row], "2025-12-01", "2025-12-31")
    assert options["elevation"] == "nan,nan"
    assert options["cell_selection"] == "sea"
    assert options["models"] == "era5"
    weather: dict[str, Any] = {
        "hourly_units": {"wind_speed_10m": "m/s"},
        "utc_offset_seconds": 0,
        "latitude": 46.25,
        "longitude": -51.25,
        "hourly": {"time": ["2025-12-06T09:00", "2025-12-06T10:00"], "wind_speed_10m": [5.0, 19.0]},
    }
    result = wind_context(row, weather)
    assert result is not None
    assert result["wind_speed_10m_m_s"] == 19.0
    assert result["time_separation_minutes"] == pytest.approx(26 + 55 / 60)
    weather["hourly_units"]["wind_speed_10m"] = "km/h"
    with pytest.raises(ValueError, match="units"):
        wind_context(row, weather)
    weather["hourly_units"]["wind_speed_10m"] = "m/s"
    weather["hourly"]["time"] = ["2025-12-05T09:00", "2025-12-05T10:00"]
    with pytest.raises(ValueError, match="thirty minutes"):
        wind_context(row, weather)


@pytest.mark.parametrize("second_name", ["acquisition-plan.json", "ACQUISITION-PLAN.json"])
def test_published_plan_names_do_not_collide(tmp_path: Path, second_name: str) -> None:
    import hashlib
    import shutil

    from validation.publish_acquisition_search import plan_output_names

    sources = [tmp_path / "first/acquisition-plan.json", tmp_path / "second" / second_name]
    for number, source in enumerate(sources):
        source.parent.mkdir()
        source.write_bytes(str(number).encode())
    names = plan_output_names(sources)
    assert len({name.casefold() for name in names}) == 2
    output = tmp_path / "publication"
    output.mkdir()
    for source, name in zip(sources, names, strict=True):
        shutil.copyfile(source, output / name)
    for source, name in zip(sources, names, strict=True):
        assert (
            hashlib.sha256((output / name).read_bytes()).digest()
            == hashlib.sha256(source.read_bytes()).digest()
        )


@pytest.fixture
def southern_publication(tmp_path: Path) -> Path:
    """Replay the owned v4 evidence contract without downloading image arrays."""
    import shutil
    import tempfile

    source = Path(__file__).resolve().parents[2] / "docs/processing-validation/v4"
    root = Path(tempfile.mkdtemp(prefix="p", dir=tmp_path.parent))
    for folder in ("acquisition-search-verified", "iw-january-rejected", "iw-december-qualified"):
        shutil.copytree(source / folder, root / folder)
    shutil.copyfile(source / "processing-manifest.json", root / "processing-manifest.json")
    return root


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_real_published_evidence_replays_without_regional_release(
    southern_publication: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, autocrlf: str
) -> None:
    import json
    import shutil
    import subprocess
    import sys

    from validation.finalise_southern_iw_report import main

    # Exercise Git's checkout of the AOI byte contract on Linux and Windows.
    project = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "checkout"
    (checkout / "configs").mkdir(parents=True)
    shutil.copyfile(project / ".gitattributes", checkout / ".gitattributes")
    aoi = checkout / "configs/aoi.geojson"
    aoi.write_bytes((project / "configs/aoi.geojson").read_bytes().replace(b"\r\n", b"\n"))
    for arguments in (
        ["init", "--quiet"],
        ["-c", f"core.autocrlf={autocrlf}", "add", ".gitattributes", "configs/aoi.geojson"],
    ):
        subprocess.run(["git", *arguments], cwd=checkout, check=True, capture_output=True)
    aoi.unlink()
    subprocess.run(
        ["git", "-c", f"core.autocrlf={autocrlf}", "checkout-index", "configs/aoi.geojson"],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(sys, "argv", ["finalise", "--report-dir", str(southern_publication)])
    main()
    report = json.loads((southern_publication / "current-status.json").read_text())
    assert report["downloaded_products"] == 4
    assert report["downloaded_archive_bytes"] == 4140235405
    assert report["qualified_modes_in_this_investigation"] == ["IW"]
    assert sum(row["passing_product_checks"] for row in report["products"]) == 1
    assert report["products"][0]["nonpositive_HV_fraction"] == pytest.approx(0.02336542177646418)
    assert report["products"][1]["scientific_comparison_passed"] is True
    assert report["products"][1]["quality_passed"] is False
    assert report["products"][2]["scientific_comparison_passed"] is None
    for gate in ("step2_complete", "deployment_gate_open", "scientific_release_granted"):
        assert report[gate] is False
    assert (southern_publication / "current-coverage.png").stat().st_size > 1000
    before = (southern_publication / "current-status.json").read_bytes()
    with pytest.raises(FileExistsError, match="Preserve"):
        main()
    assert (southern_publication / "current-status.json").read_bytes() == before


@pytest.mark.parametrize(
    "tamper", ["inventory", "receipt", "counts", "mask", "channels", "check", "windows"]
)
def test_evidence_consumer_rejects_changed_bindings_and_summaries(
    southern_publication: Path, tamper: str
) -> None:
    import json

    from cryolens.eval.cohort import file_digest
    from validation.finalise_southern_iw_report import processing_evidence

    folder = southern_publication / "iw-december-qualified"
    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    if tamper in {"inventory", "receipt"}:
        path = (
            southern_publication
            / "acquisition-search-verified/downloads"
            / (source["product"] + ".json")
        )
        value = json.loads(path.read_text())
        if tamper == "inventory":
            value["safe_files_sha256"]["manifest.safe"] = "0" * 64
        else:
            value["catalogue"]["Id"] = "changed"
        path.write_text(json.dumps(value))
        if tamper == "inventory":
            source["download_receipt_sha256"] = file_digest(path)
    elif tamper in {"counts", "mask", "channels"}:
        path = folder / "marine-quality.json"
        value = json.loads(path.read_text())
        if tamper == "counts":
            value["channels"]["HV"]["nonpositive_pixels"] += 1
        elif tamper == "mask":
            value["coastal_buffer_m"] = 0
        else:
            del value["channels"]["HV"]
        path.write_text(json.dumps(value))
    else:
        path = folder / "report.json"
        value = json.loads(path.read_text())
        if tamper == "check":
            value["products"][0]["windows"][0]["checks"]["noise_HV"]["passed"] = False
        else:
            value["products"][0]["windows"][0]["window"][0] += 1
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


@pytest.mark.parametrize(
    "tamper", ["manifest", "annotation", "missing_annotation", "extra_annotation", "inventory"]
)
def test_evidence_consumer_rejects_rehashed_scratch_receipt(
    southern_publication: Path, tamper: str
) -> None:
    import json

    from cryolens.eval.cohort import file_digest
    from validation.finalise_southern_iw_report import processing_evidence

    folder = southern_publication / "iw-december-qualified"
    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    report_path = folder / "report.json"
    report = json.loads(report_path.read_text())
    window = report["products"][0]["windows"][0]
    receipt_path = next(
        path
        for path in (folder / "reference-receipts").glob("*-receipt.json")
        if file_digest(path) == window["reference_receipt_sha256"]
    )
    receipt = json.loads(receipt_path.read_text())
    scratch = receipt["scratch_files_sha256"]
    annotation = next(name for name in scratch if name.startswith("annotation/"))
    if tamper == "manifest":
        scratch["manifest.safe"] = "0" * 64
    elif tamper == "annotation":
        scratch[annotation] = "0" * 64
    elif tamper == "missing_annotation":
        del scratch[annotation]
    elif tamper == "extra_annotation":
        scratch["annotation/extra.xml"] = "0" * 64
    else:
        del receipt["scratch_files_sha256"]
    receipt_path.write_text(json.dumps(receipt))
    window["reference_receipt_sha256"] = file_digest(receipt_path)
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Reference scratch source inventory"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


@pytest.mark.parametrize(
    ("check_name", "field", "value"),
    [
        *[
            (name, "p95_absolute_error_db", 1.0)
            for name in ("calibration_HH", "calibration_HV", "noise_HH", "noise_HV")
        ],
        ("noise_HV", "sign_mismatch_fraction", 0.0002),
        ("noise_HV", "positive_matched_pixels", 4095),
        ("noise_HV", "finite_matched_pixels", 1),
        ("noise_HV", "positive_matched_pixels", 262145),
        ("noise_HV", "positive_matched_pixels", 4096.5),
        ("noise_HV", "median_bias_db", float("nan")),
        ("noise_HV", "p95_absolute_error_db", -0.01),
        ("annotation_geolocation", "max_error_m", 20.01),
        ("annotation_geolocation", "max_error_m", float("inf")),
        ("incidence", "max_error_deg", 0.051),
        ("incidence", "max_error_deg", None),
        ("registration_HH", "relative_displacement_m", 40.01),
        ("registration_HV", "relative_displacement_m", float("nan")),
        ("registration_HH", "shift_rows_px", float("inf")),
        ("registration_HV", "shift_columns_px", float("nan")),
        ("reprojection_HH", "matched_pixels", 4095),
        ("reprojection_HV", "p95_absolute_error_db", 1.01),
        ("reprojection_HH", "p95_absolute_error_db", float("nan")),
        ("reprojection_HV", "median_bias_db", float("inf")),
        ("diagnostic_scope", "thermal_noise_corrected", True),
        ("diagnostic_scope", "fresh_detection_allowed", True),
        ("cog_integrity", "errors", ["invalid COG layout"]),
        ("cog_integrity", "warnings", "invalid warning inventory"),
        ("cog_integrity", "sha256", "g" * 64),
        ("noise_HV", "passed", 1),
    ],
)
def test_consumer_rejects_scientific_metrics_with_successful_flags(
    southern_publication: Path, check_name: str, field: str, value: Any
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    report["products"][0]["windows"][0]["checks"][check_name][field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="[Ss]cientific"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


@pytest.mark.parametrize(
    "check_name",
    [
        "calibration_HH",
        "calibration_HV",
        "noise_HH",
        "noise_HV",
        "reprojection_HH",
        "reprojection_HV",
    ],
)
@pytest.mark.parametrize("bias", [-1.0, 1.0])
def test_consumer_rejects_contradictory_residual_summaries(
    southern_publication: Path, check_name: str, bias: float
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    check = report["products"][0]["windows"][0]["checks"][check_name]
    check["median_bias_db"] = bias
    assert check["passed"] is True
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Scientific pass flag"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


@pytest.mark.parametrize("check_name", ["noise_HV", "reprojection_HV"])
@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("rounding", [False, True])
def test_consumer_accepts_residual_summary_boundaries(
    southern_publication: Path, check_name: str, sign: int, rounding: bool
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    check = report["products"][0]["windows"][0]["checks"][check_name]
    limit = 1.0 if check_name.startswith("reprojection_") else 0.02
    check["p95_absolute_error_db"] = limit
    check["median_bias_db"] = sign * (limit + (limit * 1e-8 if rounding else 0.0))
    path.write_text(json.dumps(report))
    result = processing_evidence(
        southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
    )
    assert result["passing_product_checks"] is True


@pytest.mark.parametrize(
    ("positive", "finite", "fraction"),
    [(10000, 10000, 0.0001), (9999, 10000, 0.00005), (9999, 10000, -0.0001)],
)
def test_consumer_rejects_impossible_sign_mismatch_counts(
    southern_publication: Path, positive: int, finite: int, fraction: float
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    report["products"][0]["windows"][0]["checks"]["noise_HV"].update(
        positive_matched_pixels=positive,
        finite_matched_pixels=finite,
        sign_mismatch_fraction=fraction,
    )
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Scientific pass flag"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


@pytest.mark.parametrize("bias", [-0.019, 0.019])
def test_consumer_accepts_produced_residuals_and_sign_limit(
    southern_publication: Path, bias: float
) -> None:
    import json

    from cryolens.preprocess.validation import compare_power
    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    actual = np.full(10000, 10 ** (bias / 10))
    actual[0] = -1.0
    check = compare_power(actual, np.ones(10000))
    assert check["passed"] is True
    assert check["sign_mismatch_fraction"] == 0.0001
    assert abs(check["median_bias_db"]) == check["p95_absolute_error_db"]
    report["products"][0]["windows"][0]["checks"]["noise_HV"] = check
    path.write_text(json.dumps(report))
    result = processing_evidence(
        southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
    )
    assert result["passing_product_checks"] is True


def test_consumer_accepts_existing_scientific_limits_and_cog_warnings(
    southern_publication: Path,
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    checks = report["products"][0]["windows"][0]["checks"]
    for pol in ("HH", "HV"):
        for stage in ("calibration", "noise"):
            check = checks[f"{stage}_{pol}"]
            check.update(
                positive_matched_pixels=4096,
                finite_matched_pixels=260000,
                p95_absolute_error_db=0.02,
                sign_mismatch_fraction=0.0001,
            )
        checks[f"registration_{pol}"]["relative_displacement_m"] = 40.0
        checks[f"registration_{pol}"]["shift_rows_px"] = 1.0
        checks[f"reprojection_{pol}"].update(matched_pixels=4096, p95_absolute_error_db=1.0)
    checks["annotation_geolocation"]["max_error_m"] = 20.0
    checks["incidence"]["max_error_deg"] = 0.05
    checks["cog_integrity"]["warnings"] = ["Nonfatal validation warning"]
    path.write_text(json.dumps(report))
    result = processing_evidence(
        southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
    )
    assert result["passing_product_checks"] is True


@pytest.mark.parametrize("pixels", [16, 4096])
def test_consumer_preserves_genuine_failed_scientific_observations(
    southern_publication: Path, pixels: int
) -> None:
    import json

    from cryolens.preprocess.validation import compare_power
    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    product = report["products"][0]
    window = product["windows"][0]
    window["checks"]["noise_HV"] = compare_power(np.full(pixels, 2.0), np.ones(pixels))
    assert window["checks"]["noise_HV"]["passed"] is False
    window["scientific_comparison_passed"] = False
    product["scientific_comparison_passed"] = False
    report["scientific_comparison_passed"] = False
    report["deployment_gate_open"] = False
    path.write_text(json.dumps(report))
    result = processing_evidence(
        southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
    )
    assert result["scientific_comparison_passed"] is False
    assert result["passing_product_checks"] is False


@pytest.mark.parametrize(
    "tamper",
    [
        "reported_nonpositive",
        "policy_nonpositive",
        "coverage",
        "incidence",
        "fraction",
        "nonfinite_fraction",
        "nonfinite_coverage",
        "negative_count",
        "fractional_count",
        "boolean_count",
        "too_many_finite",
        "too_many_nonpositive",
        "too_many_incidence",
        "missing_channel",
        "summary",
        "reasons",
        "stripe_gap",
        "stripe_overlap",
        "stripe_width",
        "stripe_height",
        "stripe_missing_channel",
        "stripe_fraction",
        "stripe_coverage",
        "stripe_finite",
        "stripe_incidence",
        "stripe_nonfinite",
        "aggregate_finite",
        "aggregate_incidence",
        "empty_stripes",
    ],
)
def test_consumer_rejects_matching_invalid_full_grid_quality(
    southern_publication: Path, tamper: str
) -> None:
    import copy
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    quality = report["products"][0]["quality"]
    channel = quality["channels"]["HV"]
    stripe = quality["stripes"][0]
    stripe_channel = stripe["channels"]["HV"]
    if tamper == "reported_nonpositive":
        channel["nonpositive_pixels"] = 104787441
        channel["nonpositive_fraction"] = 104787441 / channel["finite_pixels"]
    elif tamper in {"policy_nonpositive", "coverage", "incidence"}:
        for item in quality["stripes"]:
            values = item["channels"]["HV"]
            if tamper == "policy_nonpositive":
                nonpositive = values["finite_pixels"] // 4
                values["nonpositive_fraction"] = nonpositive / values["finite_pixels"]
            elif tamper == "coverage":
                values.update(
                    finite_pixels=1,
                    nonpositive_fraction=0.0,
                    valid_fraction=1 / (item["window"][2] * item["window"][3]),
                )
            else:
                values["invalid_incidence_pixels"] = 1
        channel["finite_pixels"] = sum(
            s["channels"]["HV"]["finite_pixels"] for s in quality["stripes"]
        )
        channel["nonpositive_pixels"] = sum(
            round(
                s["channels"]["HV"]["nonpositive_fraction"] * s["channels"]["HV"]["finite_pixels"]
            )
            for s in quality["stripes"]
        )
        channel["invalid_incidence_pixels"] = sum(
            s["channels"]["HV"]["invalid_incidence_pixels"] for s in quality["stripes"]
        )
        height = sum(s["window"][2] for s in quality["stripes"])
        channel["nonpositive_fraction"] = channel["nonpositive_pixels"] / channel["finite_pixels"]
        channel["valid_fraction"] = channel["finite_pixels"] / (height * stripe["window"][3])
    elif tamper in {"fraction", "nonfinite_fraction"}:
        channel["nonpositive_fraction"] = 0.0 if tamper == "fraction" else float("nan")
    elif tamper == "nonfinite_coverage":
        channel["valid_fraction"] = float("inf")
    elif tamper in {"negative_count", "fractional_count", "boolean_count", "too_many_finite"}:
        channel["finite_pixels"] = {
            "negative_count": -1,
            "fractional_count": 1.5,
            "boolean_count": True,
            "too_many_finite": 10**12,
        }[tamper]
    elif tamper in {"too_many_nonpositive", "too_many_incidence"}:
        key = (
            "nonpositive_pixels" if tamper == "too_many_nonpositive" else "invalid_incidence_pixels"
        )
        channel[key] = channel["finite_pixels"] + 1
    elif tamper == "missing_channel":
        del quality["channels"]["HH"]
    elif tamper == "summary":
        quality["passed"] = False
    elif tamper == "reasons":
        quality["reasons"] = ["recorded quality failure"]
    elif tamper in {"stripe_gap", "stripe_overlap"}:
        quality["stripes"][1]["window"][0] += 1 if tamper == "stripe_gap" else -1
    elif tamper == "stripe_width":
        quality["stripes"][1]["window"][3] -= 1
    elif tamper == "stripe_height":
        stripe["window"][2] = 0
    elif tamper == "stripe_missing_channel":
        del stripe["channels"]["HH"]
    elif tamper == "stripe_fraction":
        stripe_channel["nonpositive_fraction"] = 0.0
    elif tamper == "stripe_coverage":
        stripe_channel["valid_fraction"] = 1.0
    elif tamper == "stripe_finite":
        stripe_channel["finite_pixels"] += 1
    elif tamper == "stripe_incidence":
        stripe_channel["invalid_incidence_pixels"] = stripe_channel["finite_pixels"] + 1
    elif tamper == "stripe_nonfinite":
        stripe_channel["nonpositive_fraction"] = float("nan")
    elif tamper == "aggregate_finite":
        channel["finite_pixels"] -= 1
    elif tamper == "aggregate_incidence":
        channel["invalid_incidence_pixels"] = 1
    else:
        quality["stripes"] = []
    source["quality"] = copy.deepcopy(quality)
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="[Ff]ull-grid quality"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


def test_consumer_validates_quality_without_processing_comparisons(
    southern_publication: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import sys

    from validation.finalise_southern_iw_report import main

    path = southern_publication / "acquisition-search-verified/report.json"
    search = json.loads(path.read_text())
    source = next(row for row in search["products"] if "20260122" in row["product"])
    source["quality"]["channels"]["HV"]["nonpositive_fraction"] = 0.0
    source["quality"]["passed"] = True
    source["quality"]["reasons"] = []
    path.write_text(json.dumps(search))
    monkeypatch.setattr(sys, "argv", ["finalise", "--report-dir", str(southern_publication)])
    with pytest.raises(ValueError, match="full-grid quality"):
        main()
    assert not (southern_publication / "current-status.json").exists()


@pytest.mark.parametrize(
    ("rows", "columns", "displacement"),
    [
        (100.0, 0.0, 0.0),
        (0.0, 100.0, 0.0),
        (0.6, 0.8, 0.0),
        (0.0, 0.0, 40.0),
        (1.000000001, 0.0, 40.00000004),
    ],
)
def test_consumer_rejects_registration_shift_displacement_contradictions(
    southern_publication: Path, rows: float, columns: float, displacement: float
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    report["products"][0]["windows"][0]["checks"]["registration_HH"].update(
        shift_rows_px=rows, shift_columns_px=columns, relative_displacement_m=displacement
    )
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Scientific pass flag"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


@pytest.mark.parametrize("shift", [(0.6, 0.8), (-1.0, 0.0), (0.6, 0.79999999999)])
def test_consumer_accepts_consistent_registration_on_bound_grid(
    southern_publication: Path, shift: tuple[float, float]
) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    report["products"][0]["windows"][0]["checks"]["registration_HH"].update(
        shift_rows_px=shift[0], shift_columns_px=shift[1], relative_displacement_m=40.0
    )
    path.write_text(json.dumps(report))
    result = processing_evidence(
        southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
    )
    assert result["passing_product_checks"] is True


def test_consumer_rejects_graph_binding_mismatch(southern_publication: Path) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/report.json"
    report = json.loads(path.read_text())
    report["graph_sha256"] = "0" * 64
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Processing graph"):
        processing_evidence(
            southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
        )


def test_genuine_marine_failure_remains_rejected(southern_publication: Path) -> None:
    import json

    from validation.finalise_southern_iw_report import processing_evidence

    search = json.loads(
        (southern_publication / "acquisition-search-verified/report.json").read_text()
    )
    source = next(row for row in search["products"] if "20251206" in row["product"])
    path = southern_publication / "iw-december-qualified/marine-quality.json"
    marine = json.loads(path.read_text())
    channel = marine["channels"]["HV"]
    channel["nonpositive_pixels"] = channel["finite_pixels"]
    channel["nonpositive_fraction"] = 1.0
    marine["passed"] = False
    marine["reasons"] = ["HV: non-positive power exceeds 0.05"]
    path.write_text(json.dumps(marine))
    result = processing_evidence(
        southern_publication, source, "iw-december-qualified", "acquisition-search-verified"
    )
    assert result["scientific_comparison_passed"] is True
    assert result["marine_quality_passed"] is False
    assert result["passing_product_checks"] is False


@pytest.mark.parametrize("second_name", ["explicit.json", "EXPLICIT.json"])
@pytest.mark.parametrize("partial_download", [False, True])
def test_explicit_plan_publication_exports_plans_without_legacy_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_name: str, partial_download: bool
) -> None:
    import json
    import sys
    import xml.etree.ElementTree as ET
    from types import SimpleNamespace

    from shapely.geometry import shape

    from cryolens.eval.cohort import file_digest
    from validation import publish_acquisition_search
    from validation.screen_southern_metadata import height_value

    monkeypatch.chdir(tmp_path)
    footprint = {
        "type": "Polygon",
        "coordinates": [[[-52, 45], [-51, 45], [-51, 46], [-52, 46], [-52, 45]]],
    }
    selected = {
        "name": "planned.SAFE",
        "id": "planned-id",
        "mode": "IW",
        "date": "2025-12-06",
        "catalogue": {"GeoFootprint": footprint},
        "aoi_fraction": 1.0,
    }
    plan = tmp_path / "explicit.json"
    plan.write_text(json.dumps({"selected": [selected]}))
    second_plan = tmp_path / "followup" / second_name
    second_plan.parent.mkdir()
    second_plan.write_bytes(json.dumps({"selected": [selected]}, indent=2).encode() + b"\r\n")
    legacy = tmp_path / "data/processed/metadata-screen/old.xml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("previous unrelated selection")
    for path in (tmp_path / "docs/evaluation/v1/manifest.json", tmp_path / "configs/aoi.geojson"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    monkeypatch.setattr(publish_acquisition_search, "load_aoi", lambda: shape(footprint))
    monkeypatch.setattr(
        publish_acquisition_search.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="fixture"),
    )
    root = tmp_path / "sources"
    root.mkdir()
    if partial_download:
        annotation = root / "raw/planned/planned.SAFE/annotation/product.xml"
        annotation.parent.mkdir(parents=True)
        annotation.write_text(
            "<product><terrainHeightList><terrainHeight><value>0.0</value>"
            "</terrainHeight></terrainHeightList></product>"
        )
        heights = ET.parse(annotation).findall(".//terrainHeightList/terrainHeight")
        assert [height_value(height) for height in heights] == [0.0]
    output = tmp_path / "published"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish",
            "--root",
            str(root),
            "--output",
            str(output),
            "--plan",
            str(plan),
            "--plan",
            str(second_plan),
        ],
    )
    publish_acquisition_search.main()
    assert not (output / "metadata/metadata-screen").exists()
    assert (output / "downloads").is_dir()
    report = json.loads((output / "report.json").read_text())
    assert report["downloaded_products"] == 0
    assert report["downloaded_archive_bytes"] == 0
    assert report["products"][0]["product"] == "planned.SAFE"
    assert report["products"][0]["status"] == "download_unverified"
    assert report["products"][0]["reason"] == (
        "Download receipt missing; download completion and integrity are unverified"
    )
    assert report["products"][0]["comparison_status"] == "not_qualified"
    assert report["deployment_gate_open"] is False
    assert report["processing_validation_complete"] is False
    coverage = json.loads((output / "coverage.geojson").read_text())
    assert coverage["features"][0]["properties"]["status"] == "download_unverified"
    assert (output / "coverage.png").stat().st_size > 1000
    exports = report["consumed_plans"]
    assert len({item["published_file"].casefold() for item in exports}) == 2
    for source, item in zip([plan, second_plan], exports, strict=True):
        exported = output / item["published_file"]
        assert exported.read_bytes() == source.read_bytes()
        assert file_digest(exported) == item["sha256"] == file_digest(source)


def test_ancillary_archive_preserves_the_owned_byte_inventory() -> None:
    import hashlib
    import json
    import zipfile

    root = Path(__file__).resolve().parents[2] / "docs/processing-validation/v4"
    inventory = json.loads((root / "ancillary-evidence-inventory.json").read_text())
    archive = root / inventory["archive"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == inventory["archive_sha256"]
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == set(inventory["files_sha256"])
        for name, expected in inventory["files_sha256"].items():
            assert hashlib.sha256(z.read(name)).hexdigest() == expected
