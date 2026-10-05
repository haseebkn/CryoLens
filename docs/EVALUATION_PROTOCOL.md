# Frozen Newfoundland and Labrador evaluation protocol

Protocol **nl-mda-v1**, frozen 5 October 2026. This is a research evaluation
design. It is not an operational C-CORE protocol or a navigation product.

## Scope and separate tasks

The exact study polygon is snapshotted in
[the sealed manifest](evaluation/v1/manifest.json). It is the existing
`newfoundland_labrador_marine` polygon from `configs/aoi.geojson`, not an
authoritative provincial or maritime boundary. Pixel-centre clipping and
separate land/coast exclusions remain necessary. The footprint map is an
inventory of acquisition coverage, not proof that its water was surveyed.
The inventory admits readable AI4Arctic inputs and the three complete,
checksum-verified September 2026 SAFE archives. Partial extractions, demo outputs
and unverified downloads are excluded. The 2018 matched SAFE/AI4Arctic acquisition
is represented by its AI4Arctic input; this freeze does not checksum a directory
of extracted SAFE files as though it were a single archive.

| Task | Reference labels | What it can establish |
|---|---|---|
| Target detection | target, non_target, uncertain, unreviewed | Observable target detection in independently surveyed eligible areas |
| Sea-ice segmentation | open_water, ice_affected, unknown | Agreement with chart-derived ice context, not iceberg detection |
| Target identification | ship, iceberg, other, unknown | Identity only when independent acquisition-matched evidence supports it |

The complete machine-readable rules are
[protocol-v1.json](../configs/evaluation/protocol-v1.json). The annotation
contract is `cryolens.eval.annotations.Annotation`; records require reviewer,
UTC times, location, evidence, survey-area ID, stage, sampling stratum and
inclusion probability. It rejects task-inappropriate labels, resolved labels
without evidence, and ship/iceberg identities based only on SAR or IIP context.
Evidence provenance and analyst judgement still require human review.

A detection reference target is a compact localized return judged to represent
a discrete object at the sensor resolution. Diffuse/background clutter and
processing artefacts are non-targets; ambiguous returns remain uncertain.
Independent search must not apply the detector's minimum-component-size gate.
An observable target may still have unknown identity. These are analyst-derived
SAR references, not independently measured iceberg positions.

For segmentation, chart SIC classes 0–1 map to open water, 2–10 to ice affected,
and other values to unknown. Unknown chart values are excluded, never inferred
to be water. Chart labels have their own spatial and temporal uncertainty.

**Actual coverage.** Measured as the union of frozen metadata footprints
intersected with the study polygon, using equal-area EPSG:6931, before land,
quality or survey masks: **42%** of the polygon overall, **78%** north of 52°N
(Labrador), **24%** south of 52°N, and **2%** of a Grand Banks box (43–47°N,
47–53°W). EW HH+HV is not acquired over the Grand Banks or the NE Newfoundland
Shelf, so this inventory cannot support claims there; covering them requires
validated IW support.

## Exposure and leakage control

Existing AI4Arctic acquisitions are conservatively development-only because
their prior exploration cannot be completely reconstructed. Publisher train/test
folders are not this project's partitions. Previously published benchmark
results and chart-mask use count as analytical exposure. The
[exposure ledger](../configs/evaluation/exposure-v1.json) records known SAFE
processing and metadata/integrity-only inspection.

Connected components keep together inputs with the same original acquisition
or source hash, plus footprints within 10 km whose acquisition starts are at
most seven days apart. The distance buffer uses Canada Atlas Lambert
(EPSG:3978) and is a study grouping margin, not a target-matching tolerance.
Links are transitive. SAFE/AI4Arctic versions of the same
acquisition, all chips, polarizations, processing derivatives, and future
optical pairs inherit the parent group. Acquisitions outside the AOI are outside
this inventory; adding data requires rechecking components in a new version.

Components with analytical **or unknown** exposure cannot enter test. Within
the remaining components, a seeded SHA256 rank determines selection. At least
one clean component per available source family is reserved; this may exceed
the nominal 20% test fraction in a small inventory. Validation is selected from
the remaining groups, retaining training capacity. Validation is development
data and may be used for tuning.

**What "metadata/integrity-only" read for the September 12 test SAFE.** The
September 18 download verification did more than hash bytes: it decoded both
measurement rasters and recorded raw digital-number sample statistics, which are
min, max, unique-value count and non-zero fraction, for HH and HV
(`data/processed/downloads-20260918/verification.json`). No calibration, sigma0
conversion, CFAR, candidate extraction or visual inspection followed. These
values say nothing about target locations or detector behaviour, so they offer
nothing to tune on, and the scene remains admissible as a test. The ledger
category name understates this, and the exposure reason text will say so
explicitly in the next freeze version; v1 is not rewritten in place.

The September 11 SAFE acquisition is grouped with the previously processed
September 16 acquisition and therefore remains development-only. September 12
is reserved if it remains a separate clean component. A new publisher-labelled
AI4Arctic acquisition was downloaded solely for the freeze; see its
[publisher checksum and provenance receipt](../configs/evaluation/new-source-v1.json).
No SAR or chart arrays from this new source were inspected during setup.

The initial May 30, 2021 download was inadvertently selected by a legacy
integration fixture during regression testing. Its detection chain ran, so it
is explicitly analytical/development data. The replacement is stored under
`data/raw/evaluation_holdout/`, outside legacy archive discovery. The integration
fixture now uses only permitted development inputs. This exposure incident is
recorded rather than treating the initial download as untouched.

An April 7 replacement was also found to duplicate an existing archive input
and was rejected before admission. The acquisition novelty check now searches
the complete development archive before downloading and again before freezing.
The declared held-out chart source is January 19, 2019; it extends the pilot to
winter conditions. A winter pilot cannot establish peak-season performance.

This split reduces scene and nearby-time leakage. It does **not** establish
generalization to unseen geographic regions: the same location in a different
season may appear across partitions. Test acquisitions constitute a pilot;
broader regional evaluation requires a new, declared cohort extension.

## Matching, unknowns and area

Within one acquisition and independently surveyed eligible coverage, matching
uses maximum-cardinality one-to-one assignment with WGS84 geodesic distance
at most **200 m**, then minimum total distance. Duplicate predictions remain
unmatched. Fixed sensitivity reports use 100, 200 and 400 m; do not choose the
best threshold after inspecting test results. These are explicit study-design
choices, not externally validated maritime operating thresholds.

Uncertain or unreviewed regions are excluded from scored coverage and reported
separately. Retained-only review cannot establish recall. Rejected-candidate
sampling must retain inclusion probabilities; uncertainty estimates must account
for sampling and scene/group dependence. Missing identity evidence stays unknown.

**The six-hour window excludes dusk passes by orbit geometry.** Sentinel-1 EW
passes near 06:00 or 18:00 local solar time and Sentinel-2 near 11:00, so the
smallest achievable separation is about 5 h for dawn passes and about 7 h for
dusk passes. Dusk-pass acquisitions can therefore never receive optical
identity corroboration under this rule. In v1 that is **21 of 51 records**:
17 of 39 train and 4 of 10 validation; both test records are dawn passes.
Identity metrics will consequently describe a non-random, dawn-only subset of
acquisitions, and must be reported as such. Detection and segmentation metrics
are unaffected.

Initial optical corroboration requires acquisitions within six hours, with
visibility, resolution, displacement uncertainty and matching rationale recorded.
Clouds, movement and insufficient optical resolution can make a comparison
inconclusive. Optical absence does not establish a false radar target. IIP
associations and AIS absence do not establish iceberg identity.

Detection eligible area is the intersection of **independently surveyed**
coverage, AOI pixel centres, valid radiometry, land/coast exclusion, border/seam
exclusion, selected ice context and CFAR training-support masks. Save the exact
mask and grid metadata before scoring. Post-detection suppression does not
change this denominator. Segmentation keeps both ice and water; it cannot reuse
the detection open-water-only mask.

AI4Arctic area uses nominal pixel spacing squared and is explicitly approximate.
Projected fresh rasters need a documented ground-area method; EPSG:3978 affine
area is not assumed equal-area. Repeated acquisitions accumulate exposure;
unique acquisition footprint area is reported separately. Metadata footprint
area never substitutes for eligible surveyed water. Candidate density and
measured false-detection density remain distinct metrics.

## Reproduce and verify

Install the lightweight tracking extra without the training stack:

```powershell
uv sync --frozen --extra dev --extra tracking
python -m cryolens.eval.freeze verify
```

Verification recalculates source content hashes and acquisition partitions. It
reads bytes for integrity, not SAR/chart values. The sealed JSON detects accidental
edits; it is a content hash, not a cryptographic signature or an access-control system.
The tracked Git version is the audit trail for deliberate changes. Never rewrite
an accepted freeze in place; extend it with a new version and record why.

To rebuild the inventory in a new output folder:

```powershell
python -m cryolens.eval.freeze create --output data/interim/evaluation-reproduction
```

Reproduction requires the same raw files and recorded download-status metadata.
Scene identities, group IDs and partitions are deterministic. Code-provenance
hashes can differ when rebuilding with modified code; inspect that difference.
The published SAFE footprints are available in the manifest even on a machine
without the original status file.

## Tracking and guarded execution

```powershell
python -m cryolens.eval.freeze log
```

This creates a real local MLflow SQLite run and logs the sealed dataset manifest,
protocol, scope configuration, coverage map, Git commit, dirty-code status,
source-code hashes, a source snapshot and preprocessing digest. The snapshot
preserves the actual source even when pre-existing local changes make the Git
commit alone insufficient to reproduce it. It logs **no model performance**.
The local run receipt is `data/processed/evaluation-freeze-tracking.json`.

`tracked_experiment` is the entry point for later training/evaluation. It checks
the frozen AOI and source hashes before execution, saves the exact selected
records and explicit configuration, and records failures as failed runs. It never
logs `.env`, credential settings or the process environment. Use constructed
non-secret experiment configurations only. Tracking uses the official
[MLflow client API](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html).

The development CLI accepts only train or validation:

```powershell
python -m cryolens.eval.development --partition validation `
  --configuration configs/evaluation/development-baseline.json `
  --output data/processed/development-validation-001
```

It runs the existing AI4Arctic candidate-density benchmark under real tracking.
Fresh SAFE detection is still gated by preprocessing validation. This command is
provided for subsequent development; the freeze does not require running it.

Test access through the tracking helper requires a separate JSON release with:

```json
{
  "manifest_sha256": "<64-character frozen payload digest>",
  "protocol_sha256": "<64-character protocol digest>",
  "task": "target_detection",
  "scene_ids": ["<exact reserved test scene IDs>"],
  "model_sha256": "<64-character locked model digest>",
  "configuration_sha256": "<64-character locked configuration digest>"
}
```

The helper checks the actual supplied model file and configuration against the
release and logs the release with the run. Test access is recorded, not silently
treated as development. This does not stop someone manually opening raw files or
using legacy exploratory notebooks. Such access must be recorded and invalidates
the untouched claim. No final test release has been issued during this step.

## Still required before final results

Target/identity annotations and an independent missed-target survey remain
unfinished. The fresh SAFE processing gate remains unresolved. The new chart
test source has publisher labels, but valid NL chart coverage and performance
must be checked only after a model/configuration is locked. This small test set
cannot establish a regional false-alarm budget or operational readiness.

A full-swath development regression also exceeded available workstation
memory during CFAR. Routine regression now uses a bounded, real development
crop. Passing that check does not demonstrate full-swath memory capacity;
bounded-memory processing remains part of processing acceptance work.
