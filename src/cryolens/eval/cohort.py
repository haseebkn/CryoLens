"""Deterministic acquisition groups, sealed manifests and honest coverage accounting."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pyproj import Transformer
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import transform, unary_union

Exposure = Literal["analytical", "unknown", "metadata_integrity_only"]
Partition = Literal["train", "validation", "test"]
Task = Literal["target_detection", "sea_ice_segmentation", "target_identification"]


def digest(value: Any) -> str:
    """Hash canonical JSON independently of file formatting and dictionary order."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def file_digest(path: Path) -> str:
    """Hash actual file bytes, without reading the complete file into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acquisition_id(name: str) -> str:
    """Strip checksum/packaging/chart suffixes from an ESA acquisition identifier."""
    match = re.search(
        r"S1[A-D]_\w{2}_GRD\w_1S\w{2}_\d{8}T\d{6}_\d{8}T\d{6}_\d{6}_[A-Z0-9]{6}", name
    )
    if match is None:
        raise ValueError(f"Missing original Sentinel-1 acquisition identifier: {name}")
    return match.group()


def acquisition_time(identifier: str) -> str:
    """Read acquisition start from the original product name, in UTC."""
    match = re.search(r"\d{8}T\d{6}", identifier)
    if match is None:
        raise ValueError("Acquisition start is required")
    return datetime.strptime(match.group(), "%Y%m%dT%H%M%S").replace(tzinfo=UTC).isoformat()


class SceneRecord(BaseModel):
    """Metadata and content identity for one input; no labels or SAR values read."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scene_id: str
    acquisition_id: str
    acquired_utc: str
    family: Literal["ai4arctic", "sentinel1_safe"]
    source_path: str
    source_aliases: list[str] = Field(default_factory=list)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_bytes: int = Field(gt=0)
    footprint: dict[str, Any]
    footprint_method: str
    label_status: str
    exposure: Exposure
    exposure_reason: str
    group_id: str = ""
    partition: Partition = "train"

    @model_validator(mode="after")
    def valid_geometry_and_time(self) -> SceneRecord:
        geometry = shape(self.footprint)
        if geometry.is_empty or not geometry.is_valid or geometry.geom_type != "Polygon":
            raise ValueError("Scene footprint must be a valid nonempty polygon")
        if not (-180 <= geometry.bounds[0] <= geometry.bounds[2] <= 180):
            raise ValueError("Footprint longitude outside WGS84 range")
        if not (-90 <= geometry.bounds[1] <= geometry.bounds[3] <= 90):
            raise ValueError("Footprint latitude outside WGS84 range")
        when = datetime.fromisoformat(self.acquired_utc)
        if when.tzinfo is None:
            raise ValueError("Acquisition time needs a timezone")
        for path in [self.source_path, *self.source_aliases]:
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("Source paths must be relative to the dataset root")
        return self


def assign_partitions(records: list[SceneRecord], protocol: dict[str, Any]) -> list[SceneRecord]:
    """Keep transitive spatiotemporal links and acquisition duplicates together.

    Test is selected only from unexposed components. Unknown exposure is never
    treated as proof that an acquisition is untouched. Hash ranking is stable
    under input permutation; changing inventory requires a new freeze version.
    """
    if len({r.scene_id for r in records}) != len(records):
        raise ValueError("Duplicate scene identifiers")
    rules = protocol["grouping"]
    split = protocol["partitioning"]
    gap = float(rules["temporal_gap_days"])
    buffer = float(rules["spatial_buffer_m"])
    test_fraction = float(split["test_fraction"])
    validation_fraction = float(split["validation_fraction"])
    if gap < 0 or buffer < 0 or not (0 < test_fraction < 1 and 0 < validation_fraction < 1):
        raise ValueError("Invalid grouping or split fractions")
    if test_fraction + validation_fraction >= 1:
        raise ValueError("Partitions must leave training capacity")
    ordered = sorted(records, key=lambda r: r.scene_id)
    parents = list(range(len(ordered)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    project = Transformer.from_crs("EPSG:4326", rules["projection"], always_xy=True)
    geometries = [transform(project.transform, shape(r.footprint)) for r in ordered]
    times = [datetime.fromisoformat(r.acquired_utc) for r in ordered]
    for i, first in enumerate(ordered):
        for j in range(i):
            second = ordered[j]
            duplicate = (
                first.acquisition_id == second.acquisition_id
                or first.source_sha256 == second.source_sha256
            )
            nearby = abs((times[i] - times[j]).total_seconds()) <= gap * 86400
            if duplicate or (nearby and geometries[i].distance(geometries[j]) <= buffer):
                parents[root(i)] = root(j)
    groups: dict[int, list[SceneRecord]] = {}
    for i, record in enumerate(ordered):
        groups.setdefault(root(i), []).append(record)
    components = list(groups.values())

    def rank(group: list[SceneRecord]) -> str:
        return digest([split["seed"], sorted({r.acquisition_id for r in group})])

    clean = sorted(
        (g for g in components if all(r.exposure == "metadata_integrity_only" for r in g)),
        key=rank,
    )
    n_test = max(1, math.ceil(len(clean) * test_fraction)) if clean else 0
    test_ids = {rank(g) for g in clean[:n_test]}
    # Cover each available input family without selecting by labels or performance.
    for family in sorted({r.family for g in clean for r in g}):
        representative = next(g for g in clean if any(r.family == family for r in g))
        test_ids.add(rank(representative))
    development = sorted((g for g in components if rank(g) not in test_ids), key=rank)
    # Always retain training; a singleton development group cannot also validate.
    n_validation = min(len(development) - 1, math.ceil(len(development) * validation_fraction))
    validation_ids = {rank(g) for g in development[: max(0, n_validation)]}
    output: list[SceneRecord] = []
    for group in components:
        identifier = rank(group)
        partition: Partition = (
            "test"
            if identifier in test_ids
            else "validation"
            if identifier in validation_ids
            else "train"
        )
        output.extend(
            r.model_copy(update={"group_id": identifier, "partition": partition}) for r in group
        )
    return sorted(output, key=lambda r: r.scene_id)


def read_aoi(path: Path, feature_id: str) -> dict[str, Any]:
    """Snapshot the primary feature, avoiding changes to the live configuration."""
    features = json.loads(path.read_text(encoding="utf-8"))["features"]
    feature = next(f for f in features if f["properties"]["id"] == feature_id)
    geometry = shape(feature["geometry"])
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("Invalid AOI")
    return dict(feature)


def inventory_ai4arctic(
    root: Path,
    aoi: dict[str, Any],
    exposure: dict[str, Any],
    reserved_source: Path | None = None,
) -> list[SceneRecord]:
    """Read geolocation tie points and metadata only; never read ice/SAR arrays.

    A perimeter through the coarse tie points approximates acquisition coverage,
    unlike a scene-centre rectangle. The full pipeline still clips pixel centres.
    """
    from netCDF4 import Dataset

    records = []
    boundary = shape(aoi["geometry"])
    paths = list((root / "ai4arctic").rglob("*_prep.nc"))
    if reserved_source is not None:
        if any((root / "ai4arctic").rglob(reserved_source.name)):
            raise ValueError("Reserved source duplicates development archive input")
        paths.append(reserved_source)
    for path in sorted(paths):
        with Dataset(str(path)) as dataset:
            lat = np.ma.filled(dataset.variables["sar_grid2d_latitude"][:], np.nan)
            lon = np.ma.filled(dataset.variables["sar_grid2d_longitude"][:], np.nan)
            grid = np.stack([lon, lat], axis=-1)
            perimeter = np.concatenate([grid[0], grid[1:, -1], grid[-1, -2::-1], grid[-2:0:-1, 0]])
            if not np.isfinite(perimeter).all():
                raise ValueError(f"Incomplete footprint tie points: {path.name}")
            geometry = Polygon(perimeter)
            if not geometry.is_valid:
                raise ValueError(f"Invalid tie-point perimeter: {path.name}")
            if geometry.intersection(boundary).area <= 0:
                continue
            identifier = acquisition_id(str(dataset.original_id))
            supplied_split = path.parent.name
            has_chart_variable = "SIC" in dataset.variables
        evidence = exposure["acquisitions"].get(identifier, {})
        records.append(
            SceneRecord(
                scene_id=path.name,
                acquisition_id=identifier,
                acquired_utc=acquisition_time(identifier),
                family="ai4arctic",
                source_path=path.relative_to(root).as_posix(),
                source_sha256=file_digest(path),
                source_bytes=path.stat().st_size,
                footprint=mapping(geometry),
                footprint_method="coarse_geolocation_tie_point_perimeter",
                label_status=(
                    "publisher_withheld"
                    if supplied_split == "test"
                    else "chart_proxy_available"
                    if has_chart_variable
                    else "no_reference_labels"
                ),
                exposure=evidence.get("exposure", exposure["family_defaults"]["ai4arctic"]),
                exposure_reason=evidence.get(
                    "reason", "Existing archive analytical exposure cannot be ruled out."
                ),
            )
        )
    unique: dict[str, SceneRecord] = {}
    priority = {"metadata_integrity_only": 0, "unknown": 1, "analytical": 2}
    for record in records:
        previous = unique.get(record.scene_id)
        if previous is None:
            unique[record.scene_id] = record
            continue
        if (
            previous.source_sha256 != record.source_sha256
            or previous.acquisition_id != record.acquisition_id
        ):
            raise ValueError(f"Conflicting duplicate scene input: {record.scene_id}")
        strongest = max((previous, record), key=lambda r: priority[r.exposure])
        unique[record.scene_id] = previous.model_copy(
            update={
                "source_aliases": sorted({*previous.source_aliases, record.source_path}),
                "exposure": strongest.exposure,
                "exposure_reason": strongest.exposure_reason,
            }
        )
    return list(unique.values())


def inventory_safe(
    root: Path, status_path: Path, aoi: dict[str, Any], exposure: dict[str, Any]
) -> list[SceneRecord]:
    """Use recorded catalogue footprints and recompute archive content hashes."""
    status = json.loads(status_path.read_text(encoding="utf-8"))
    records = []
    boundary = shape(aoi["geometry"])
    for product in status["products"]:
        if (
            product["state"] != "complete"
            or shape(product["footprint"]).intersection(boundary).area <= 0
        ):
            continue
        name = product["name"]
        path = root / "safe" / "archives" / (name.removesuffix(".SAFE") + ".zip")
        sha = file_digest(path)
        if sha != product["archive_sha256"] or path.stat().st_size != product["catalogue_bytes"]:
            raise ValueError(f"SAFE archive differs from verified download: {name}")
        identifier = acquisition_id(name)
        evidence = exposure["acquisitions"].get(identifier, {})
        records.append(
            SceneRecord(
                scene_id=name,
                acquisition_id=identifier,
                acquired_utc=acquisition_time(identifier),
                family="sentinel1_safe",
                source_path=path.relative_to(root).as_posix(),
                source_sha256=sha,
                source_bytes=path.stat().st_size,
                footprint=product["footprint"],
                footprint_method="recorded_cdse_catalogue_polygon",
                label_status="no_reference_labels",
                exposure=evidence.get("exposure", exposure["family_defaults"]["sentinel1_safe"]),
                exposure_reason=evidence.get("reason", "Unknown analytical exposure."),
            )
        )
    return records


def seal(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach a content digest; this detects edits, it is not a signature."""
    return {"payload": payload, "sha256": digest(payload)}


def load_manifest(path: Path) -> dict[str, Any]:
    """Reject modified files and recompute all acquisition groups/partitions."""
    sealed = json.loads(path.read_text(encoding="utf-8"))
    payload = sealed["payload"]
    if digest(payload) != sealed["sha256"]:
        raise ValueError("Frozen manifest digest mismatch")
    if payload["schema_version"] != 1:
        raise ValueError("Unsupported manifest schema")
    if digest(payload["protocol"]) != payload["protocol_sha256"]:
        raise ValueError("Protocol digest mismatch")
    if digest(payload["aoi"]) != payload["aoi_sha256"]:
        raise ValueError("AOI digest mismatch")
    records = [SceneRecord.model_validate(r) for r in payload["scenes"]]
    expected = assign_partitions(records, payload["protocol"])
    if [r.model_dump(mode="json") for r in expected] != payload["scenes"]:
        raise ValueError("Manifest partitions disagree with frozen grouping rules")
    return dict(payload)


def select_partition(
    manifest: dict[str, Any],
    partition: Partition,
    task: Task,
    release: dict[str, Any] | None = None,
) -> list[SceneRecord]:
    """Fail closed on test access until a locked evaluation release is supplied."""
    if task not in manifest["protocol"]["tasks"]:
        raise ValueError("Unknown evaluation task")
    records = [
        SceneRecord.model_validate(r) for r in manifest["scenes"] if r["partition"] == partition
    ]
    if partition == "test":
        if not release or release.get("manifest_sha256") != digest(manifest):
            raise ValueError("Test access requires a release binding this manifest")
        if (
            release.get("protocol_sha256") != manifest["protocol_sha256"]
            or release.get("task") != task
        ):
            raise ValueError("Release task/protocol mismatch")
        if sorted(release.get("scene_ids", [])) != sorted(r.scene_id for r in records):
            raise ValueError("Release must name the exact test acquisitions")
        for key in ("model_sha256", "configuration_sha256"):
            if not re.fullmatch("[a-f0-9]{64}", str(release.get(key, ""))):
                raise ValueError(f"Release needs a locked {key}")
    if task == "sea_ice_segmentation":
        records = [r for r in records if r.label_status == "chart_proxy_available"]
    if not records:
        raise ValueError(
            f"Empty eligible {partition} partition for {task}; accessible reference labels may be missing"
        )
    return records


def verify_sources(records: list[SceneRecord], root: Path) -> None:
    """Verify consumed data against frozen content identity before an experiment."""
    for record in records:
        for name in [record.source_path, *record.source_aliases]:
            path = (root / name).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Dataset path escaped its root")
            if (
                path.stat().st_size != record.source_bytes
                or file_digest(path) != record.source_sha256
            ):
                raise ValueError(f"Dataset source changed: {record.scene_id}")


def coverage_features(records: list[SceneRecord], aoi: dict[str, Any]) -> dict[str, Any]:
    """Export clipped acquisition footprints; area is not surveyed eligible water."""
    boundary = shape(aoi["geometry"])
    project = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    footprints = []
    features = [aoi]
    for record in records:
        geometry = shape(record.footprint).intersection(boundary)
        footprints.append(geometry)
        features.append(
            {
                "type": "Feature",
                "geometry": mapping(geometry),
                "properties": {
                    "scene_id": record.scene_id,
                    "group_id": record.group_id,
                    "partition": record.partition,
                    "acquired_utc": record.acquired_utc,
                    "family": record.family,
                    "footprint_area_km2": transform(project.transform, geometry).area / 1e6,
                    "area_kind": "approximate_acquisition_footprint_not_eligible_water",
                },
            }
        )
    unique = unary_union(footprints)
    # Source derivatives must not count as additional acquisition exposure.
    per_acquisition: dict[str, list[Any]] = {}
    for record, geometry in zip(records, footprints, strict=True):
        per_acquisition.setdefault(record.acquisition_id, []).append(geometry)
    cumulative = sum(
        transform(project.transform, unary_union(g)).area / 1e6 for g in per_acquisition.values()
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "summary": {
            "unique_acquisitions": len(per_acquisition),
            "source_records": len(records),
            "unique_footprint_km2": transform(project.transform, unique).area / 1e6,
            "cumulative_acquisition_footprint_km2": cumulative,
            "eligible_surveyed_area_km2": None,
            "area_method": "EPSG:6933 area of metadata polygons; land, quality and survey masks not applied",
        },
    }


def write_frozen_bundle(directory: Path, payload: dict[str, Any]) -> None:
    """Create a version exactly once. Reproduction uses a different output folder."""
    if directory.exists():
        raise FileExistsError(
            "Frozen output already exists; use a new version/reproduction directory"
        )
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps(seal(payload), indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    records = [SceneRecord.model_validate(r) for r in payload["scenes"]]
    coverage = coverage_features(records, payload["aoi"])
    (directory / "coverage.geojson").write_text(
        json.dumps(coverage, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    render_coverage(records, payload["aoi"], directory / "coverage.png")


def render_coverage(records: list[SceneRecord], aoi: dict[str, Any], output: Path) -> None:
    """Draw actual metadata footprints by partition; do not draw eligible pixels."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    colors = {"train": "#2878b5", "validation": "#f1a340", "test": "#9b59b6"}
    figure, axis = plt.subplots(figsize=(8, 9))
    for record in sorted(
        records, key=lambda r: {"train": 0, "validation": 1, "test": 2}[r.partition]
    ):
        geometry = shape(record.footprint).intersection(shape(aoi["geometry"]))
        pieces = [geometry] if geometry.geom_type == "Polygon" else geometry.geoms
        for piece in pieces:
            if piece.geom_type == "Polygon":
                x, y = piece.exterior.xy
                axis.fill(x, y, color=colors[record.partition], alpha=0.12)
                axis.plot(
                    x,
                    y,
                    color=colors[record.partition],
                    linewidth=1.5 if record.partition == "test" else 0.5,
                )
    boundary = shape(aoi["geometry"])
    x, y = boundary.exterior.xy
    axis.plot(x, y, color="black", linewidth=1.5)
    axis.set(
        xlabel="Longitude (degrees east)",
        ylabel="Latitude (degrees north)",
        title="NL study area and acquisition footprints\nMetadata coverage; land/quality/survey masks not applied",
    )
    axis.set_aspect(1 / math.cos(math.radians(52)))
    axis.legend(handles=[Patch(color=c, label=p) for p, c in colors.items()])
    axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(output, dpi=150)
    plt.close(figure)
