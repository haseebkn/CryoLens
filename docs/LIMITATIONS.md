# Known limitations

Reviewed 2026-09-05. CryoLens is a research screening and review project, not an
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

### Validation outcome, 2026-09-16

The reader has now been run against a real product: the S1B EW GRDM HH+HV
acquisition of 2018-04-28T09:39:37 over the Labrador Shelf, which is the same
acquisition NERSC processed for the AI4Arctic archive, giving an independent
reference for the identical scene.

| Property | CryoLens | NERSC reference | Outcome |
|---|---|---|---|
| Geolocation extent | 48.87-53.16 N, 54.61-47.48 W | identical | Confirmed |
| Incidence angle | 19.33-47.41 deg | 19.63-46.65 deg | Confirmed |
| HH median sigma0 | -19.29 dB | -20.67 dB | Agrees within 1.4 dB |
| HV median, before noise removal | -30.60 dB | -29.02 dB | Agrees within 1.6 dB |
| HV median, after ESA noise removal | -33.96 dB | -29.02 dB | **Diverges by 4.9 dB** |

**Calibration and geolocation are confirmed. ESA standard thermal noise removal
is not usable here.** Subtracting the ESA noise vectors drove 45.7 percent of HV
and 10.7 percent of HH pixels to non-positive power on this scene. Non-positive
power has no decibel representation and cannot enter a CFAR statistic, so those
pixels are lost rather than merely noisy. This is the failure ADR-007 predicted
for low-backscatter maritime cross-pol, now measured rather than anticipated.

The reader reports `nonpositive_power_fraction` and `usable_for_cfar` on every
read and warns above 5 percent, so an over-subtracted channel cannot be consumed
as though it were calibrated backscatter. Making the raw SAFE path usable for
detection requires NERSC-style denoising; that is not yet implemented, and the
measured results in BENCHMARK.md do not depend on this path.

Precise and restituted orbit products (POEORB/RESORB) can be acquired without
credentials from the public ESA STEP and ASF auxiliary-data mirrors, selected by
the validity window encoded in the product filename, and validated as parseable
XML carrying state vectors before they are cached. **Acquiring an orbit is not
applying one.** Geolocation remains that recorded in the product annotation, no
correction is computed, and `orbit_correction_applied` stays `False` in
provenance. Applying a precise orbit means recomputing geolocation, which cannot
responsibly be implemented until the resulting geolocation can be checked
against trusted processing. The calibration comparison above is now done; an
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
