# Known limitations

The [independent reference pilot](reference-set/v1/README.md) now has real
survey packets, a full development raw-component frame, probability samples of
rejected components, and a human review/repeat/adjudication workflow. It has
**zero accepted human responses** at preparation. Surveyed coverage, reviewer
consistency, reference labels and effort estimates are therefore unmeasured.
An independent second reviewer has not been obtained. Prepared imagery and
available-area counts must not be reported as completed annotation coverage.

Reviewed 2026-10-05. CryoLens is a research screening and review project, not an
operational iceberg surveillance service. The current benchmark evidence and
its exclusions are described in [BENCHMARK.md](BENCHMARK.md).

## 1. False positives and missed targets are not measured

There is no independently adjudicated, acquisition-matched iceberg reference
set in this project. Candidate density is count divided by analyzed area. It
cannot identify which candidates are false or how many real icebergs were
missed. Within a fixed counted set, false detections cannot exceed all
detections, but that arithmetic does not validate a predictive false-alarm
rate on new scenes. The old assertion that open-water candidates are dominated
by false alarms was not supported by labels and has been withdrawn.

Low counts can be caused by poor sensitivity, aggressive masking, incorrect
units or genuine target scarcity. The minimum connected-component filter
removes small returns, including potentially real small icebergs. Conservative
open-water screening trades coverage and sensitivity for fewer ambiguous
returns. It cannot establish that masked areas are clear of hazards.

## 2. Context is not identity

AI4Arctic ice charts describe sea ice, not individual icebergs. IIP sightings
provide regional and temporal context; positional uncertainty and drift make
naive pixel overlap unsuitable as truth. Ships and offshore structures can
produce bright SAR returns. Live AIS and structure inventories are not
integrated; `not_checked` must never be read as a negative match.

Automated candidates are unclassified. A heuristic detector score is not
calibrated iceberg confidence and is not a MANICE confidence code. Analyst
judgments are attributed review evidence, not automatically independent
physical ground truth.

Sentinel-2 paired review now supplies real RGB/NIR/SCL crops and independent
optical evidence records ([availability report](optical-review/v1/README.md)).
The two-scene pilot has no candidate passing the visibility screen. Tile
footprints can contain extensive nodata; a catalogue match is not clear imagery.
The 20 m SCL layer can confuse ice and clouds and cannot resolve every small
object visible in 10 m bands. Time-separated moving targets are not expected
at the same pixel. The default movement and geolocation allowances are research
assumptions, not measured accuracy or a validated vessel/iceberg drift bound.
Chips are capped at 6 km radius and explicitly flag larger assumed envelopes.
An absent counterpart never automatically changes the radar verdict.

The [CDSE alternative-provider verification](optical-review/cdse-v1/README.md)
verifies original SAFE bytes, but the available acquisition is reprocessed
from baseline 02.12 to 05.00. Matching acquisition, orbit and tile does not
establish product or pixel equivalence. BOA offsets are read from each original
product's XML, and radiometric/SCL differences remain after offset correction.
The CDSE one-candidate pilot also fails the useful-visibility screen. Alternative
access does not supply missing target truth or validate fresh SAR processing.

## 3. Data units and labels

Ready-to-train AI4Arctic fields have been standardized by the publisher.
Physical backscatter requires the publisher's normalization constants; a
scene's observed min/max cannot be assumed to be the transformation bounds.
Unknown calibration must fail rather than infer plausible-looking decibels.
Coarse geolocation tie points and sea-ice charts add uncertainty. Held-out
ice-chart labels can be absent. Unknown sea ice must remain unknown.

## 4. Spatial coverage and area

The NL shelf polygon is a hand-defined study design, not an official provincial
boundary or EEZ. It focuses the analysis around Newfoundland and Labrador and
excludes distant regions covered by the former broad rectangle. Land masking
still requires an independent coastline. Pixel spacing is not spatial
resolution. The ready-to-train pixels are nominally 80 m and native EW GRD
spacing is about 40 m; apparent component size is not physical iceberg size.

Area derived from nominal pixel spacing is an approximation. Repeated scene
coverage is accumulated analyzed area, not unique ocean area. Results must
include actual analyzed coverage after masking and identify excluded scenes.
A restricted subset does not establish performance for every season or region.

## 5. Statistical and masking assumptions

CA-CFAR assumes a background model; Gamma-CFAR fits a Gamma law, not a full
K-distribution. Nominal Pfa is a model setting, not empirical scene-level FAR.
Correlated SAR looks, heterogeneous clutter and uncertain preprocessing can
violate those assumptions. Thresholds and component-size gates are not
validated operating points. Seams inferred from image gradients can confuse
physical ice boundaries with artifacts. Masks can suppress real targets.

## 6. Raw SAFE processing

The Python path reads actual measurements, calibration and noise LUTs and
uses annotation geolocation. It does not apply precise orbit correction or
constitute a validated terrain-corrected operational chain. It requires
comparison with trusted processing on real SAFE products. Running SNAP and
then ignoring its output is no longer presented as a successful SNAP pipeline.
Unsupported engines and missing inputs fail explicitly.

### Validation outcome, 2026-10-05

The [reproducible processing report](processing-validation/v1/README.md)
supersedes the earlier scene-median comparison. Similar extents and medians
did not confirm calibration or absolute geolocation accuracy.

Actual SNAP 14.0.0 comparisons use matched sensor pixels and systematic spatial
windows on legacy EW, fresh EW and fresh IW products. Calibration and signed
noise subtraction agree closely after two interpolation fixes. Full-footprint
HV non-positive fractions nevertheless remain about 45.7%, 26.1% and 54.3%,
respectively. All three fail the conservative project quality preset. Negative
residuals are retained for diagnostics; no positive floor manufactures data.

Fresh EW windows pass the tested geometric comparisons, but legacy EW and IW
retain spatial/resampled-radiometry discrepancies. Common annotation agreement
does not establish absolute accuracy. The fresh SAFE deployment gate remains
closed, and rejected products write machine-readable reasons before detection.
The 5% non-positive threshold is a research acceptance preset, not a published
ESA or C-CORE requirement.

The published NERSC algorithm was evaluated on the legacy Sentinel-1B scene.
With negative-gap filling disabled, 59.6% of HV remained non-positive. It is not
adopted or assumed to solve this problem. Sentinel-1C/D coefficient qualification
and a defensible noise workflow remain open. Benchmark results in BENCHMARK.md
use the separate AI4Arctic processing path and do not depend on fresh SAFE data.

Precise and restituted orbit products (POEORB/RESORB) can be acquired without
credentials from the public ESA STEP and ASF auxiliary-data mirrors, selected by
the validity window encoded in the product filename, and validated as parseable
XML carrying state vectors before they are cached. **Acquiring an orbit is not
applying one.** Geolocation remains that recorded in the product annotation, no
correction is computed, and `orbit_correction_applied` stays `False` in
provenance. Applying a precise orbit means recomputing geolocation, which cannot
responsibly be implemented until the resulting geolocation can be checked
against trusted processing. Matched calibration comparisons are now done; an
orbit-correction comparison is not, and remains future work.

## 7. Machine learning

There is no trained YOLO or ship/iceberg classifier. Training chip export
requires real pixels and final analyst verdicts. Scene-disjoint train/validation
partitions prevent direct scene leakage; broader temporal and spatial
independence still needs study. A Kaggle chip classifier result would not by
itself validate detection on full NL satellite scenes.

## 8. Forecasts are unavailable

Drift output is disabled, including legacy API tracks. The previous constant
currents, winds and bathymetry were invented and could not support a forecast.
Installing OpenDrift does not supply physical iceberg parameters, matched
forcing or validated prediction skill. These integrations remain future work.

## 9. Application and deployment

The dashboard is a local portfolio/analyst tool with optional API-key protected
review writes. Enterprise identity, per-user roles, deployment hardening,
backup/recovery and operational latency guarantees are not implemented.
Unit tests replace some spatial behavior with SQLite substitutes; the separate
real PostGIS check and CI service cover the actual spatial query path.

## 10. No operational certification

No C-CORE internal protocol, certification, endorsement or proprietary
implementation is claimed. GeoJSON exchange is not a claim of MANICE or STAC
compliance. CryoLens outputs are not navigation products, ice warnings,
search-and-rescue advice or assertions of suspicious/illegal vessel activity.
