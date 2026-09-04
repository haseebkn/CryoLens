# CryoLens — Limitations

This document states what CryoLens does **not** do, what its numbers do **not**
mean, and where the physics or the data run out. It is written for a reviewer
who needs to know how far the results can be trusted before relying on them.

Nothing here is hidden in a footnote elsewhere. If a claim in the README or
`BENCHMARK.md` has a caveat, that caveat is stated here in full.

---

## 1. There is no iceberg ground truth in the measured results

**This is the single most important limitation.**

The benchmark reports **detection density per 1000 km² of analysed water**. It
does *not* report precision, recall, F1, or mAP, because no verified iceberg
positions exist for the scenes analysed.

- The AI4Arctic dataset provides **ice charts** (concentration, stage of
  development, floe size) — polygon products describing sea ice, not point
  observations of icebergs.
- The IIP database (NSIDC G00807) provides iceberg sightings, but they cannot be
  intersected with these scenes for the reasons in §2.
- xView3-SAR provides AIS-matched **vessel** annotations, not icebergs.

**What the density number therefore means:** over open water, far from land and
ice, at Sentinel-1 EW resolution, genuine icebergs are sparse. Detection density
in that stratum is therefore dominated by false alarms and is reported as a
defensible **upper bound on the false-alarm rate**. It is an upper bound, not an
estimate: some fraction of those detections are real icebergs, ships, or
offshore structures, and the harness cannot separate them.

Any claim of the form "CryoLens detects X% of icebergs" is unsupported by
anything in this repository.

## 2. IIP sightings cannot be used as pixel-level labels

Icebergs drift at roughly 0.1–0.5 m/s. A six-hour offset between an IIP sighting
and a satellite overpass corresponds to **2–11 km** of displacement, which at
40 m GRD pixel spacing is 50–270 pixels. IIP positions also come largely from
aerial and shipboard reconnaissance with their own position uncertainty.

Consequently IIP is usable only as a **weak-supervision search prior** — filter
sightings to ±Δt, buffer by `v_max · Δt` plus position uncertainty, and mine
detections inside the buffer for human validation. It is never a label.

A further hard limit: Sentinel-1 EW GRD (40 m spacing, ~90 m resolution) cannot
resolve IIP **Growlers** (<5 m) or **Bergy Bits** (5–15 m). Any recall metric
computed against the full IIP size distribution is meaningless; metrics must be
stratified by size class, and the two smallest classes excluded.

## 3. Absolute wind speed is not recoverable from the benchmark scenes

The AI4Arctic ready-to-train distribution standardises every variable. For the
two SAR channels the packagers preserved the pre-normalisation extremes in the
`min`/`max` variable attributes, so physical σ⁰ in decibels is recovered exactly.
**The ERA5 wind fields carry no such attributes**, so metres per second cannot be
restored from the file alone.

Wind stratification is therefore reported in **relative terciles across the
scene cohort** — each scene's median wind magnitude is ranked against the other
scenes in the run — not in absolute m/s bins. This still separates calm from
roughened sea-surface regimes, which is what drives ocean clutter, but it is not
comparable to a published FAR-versus-wind-speed curve, and the boundaries move
if the scene set changes. Restoring absolute values requires fetching ERA5
directly.

Note that the binning must be *between* scenes. An earlier implementation
compared each scene's median against its own quantiles, which returns "moderate"
for every scene by construction, and produced a stratification table with a
single row covering the whole run.

## 4. Incidence angle is approximate in the benchmark scenes

For the same reason, incidence angle is restored by mapping the standardised
extremes onto the nominal Sentinel-1 EW swath range (19.4°–47.0°). This is exact
only if a scene spans the full swath. Scenes that do not will have a compressed
and slightly wrong incidence ramp.

This affects the incidence-angle band of the feature stack and any
incidence-stratified reporting. It does **not** affect the CFAR detector, which
operates on cross-pol backscatter and estimates its threshold locally.

The `SAFEProductReader` path reads true incidence angles from the product
annotation and is not subject to this limitation.

## 5. The SAFE reader has not been validated against a real product

`cryolens/preprocess/safe_reader.py` implements calibration to σ⁰ and thermal
noise removal per the ESA Sentinel-1 Product Specification, and is unit-tested
against a synthetic SAFE product with known digital numbers. **It has not yet
been run against a real `.SAFE` download**, because Sentinel-1 EW HH+HV over the
Labrador Shelf requires CDSE or NASA Earthdata credentials that are not present
in this environment. Microsoft Planetary Computer carries only IW mode for this
region.

Until that validation runs, treat the SAFE path as *implemented but unproven*.
The measured results in `BENCHMARK.md` do not depend on it — they come from the
AI4Arctic reader, which operates on real Sentinel-1 EW data.

## 6. Point-level historical AIS is not available for this region

High-resolution historical vessel AIS over the Grand Banks is proprietary. NOAA
Marine Cadastre covers US waters only. Consequences:

- Vessel labels, when training resumes, come from xView3-SAR's AIS-matched
  annotations, not from a local AIS feed.
- The live AIS correlation step is an **architected interface with no data
  behind it**. `eval/correlate.py` implements the spatiotemporal matching logic;
  the vessel feed itself is a documented integration point for a commercial
  provider (Spire, exactEarth, MarineTraffic).
- Iceberg/ship discrimination therefore rests on polarimetric behaviour
  (HH/HV ratio, volume versus specular scattering) rather than on identity
  confirmation.

## 7. No trained deep learning model exists

`detect/yolo.py` is an **interface only** and raises `NotImplementedError`. An
earlier revision returned a hardcoded synthetic detection, which made the
benchmark meaningless; it was removed rather than improved.

Training requires xView3-SAR and the Statoil/C-CORE Kaggle chips, both behind
terms acceptance. The architecture also needs real modification before it could
work at this scale: 256 px tiles, a P2 detection head, a 4-channel input stem,
and mosaic augmentation disabled — at 40 m spacing a 100 m iceberg is 2–3 pixels
across and YOLOv8's finest stride is 8.

**The only measured detector in this project is CFAR.**

## 8. Drift forecasting is scaffolded, not validated

`drift/` wires OpenDrift's `openberg` model, but:

- `opendrift` is not a declared dependency, so `run_forecast` currently raises.
  It previously fell through to a mock trajectory that was written to
  `drift_forecasts` and served through `/api/v1/drift/{id}` indistinguishable
  from a real forecast; that fallback was removed under ADR-012. An explicitly
  labelled `synthetic_trajectory()` remains for interface testing, and stamps
  every waypoint with `"synthetic": True`.
- Ocean and wind forcing use **constant synthetic readers**, not CMEMS or ERA5.
- Bathymetry uses a **constant 80 m depth**, not CHS NONNA-100. Grounding
  detection on the Grand Banks is therefore not meaningfully implemented.
- There is no residual XGBoost correction and no validation against IIP
  resightings, persistence, or pure current advection.

No drift number produced by this repository should be relied upon.

## 9. Suppression thresholds are physically motivated, not optimally tuned

The false-alarm suppression chain uses fixed thresholds chosen from SAR physics
and Sentinel-1 EW resolution limits (see `detect/filters.py` docstrings). They
have **not** been optimised against a labelled validation set, because no such
set exists (§1).

In practice one stage — the minimum connected-component size — does most of the
work, because CFAR at Pfa = 1e-5 produces predominantly isolated single-pixel
speckle hits while real targets form multi-pixel clusters. The remaining stages
contribute comparatively little on the scenes analysed. This is visible in the
suppression ledger and is not concealed by an aggregate number.

**The consequence is a recall cost that is not measured.** Raising
`min_target_pixels` reduces false alarms and simultaneously discards small
icebergs. Where that trade sits cannot be quantified without ground truth.

## 10. Subswath seam detection is empirical and capped

Seams are found as outliers in the gradient of the column-median cross-pol
profile, rather than from subswath geometry, which does not survive resampling.

This is robust to reprojection but can be fooled: a sharp ice edge running
across range produces the same signature. A ceiling (`max_seam_fraction`,
default 5%) limits the damage — one Labrador scene tripped the test across 34.7%
of its columns before the cap was added — but a scene with strong large-scale
radiometric structure may still lose usable water to seam masking.

## 11. Geographic and temporal scope

- **Region:** the analysed scenes are Newfoundland and Labrador only, filtered
  by scene centre inside the AOI. Most are Labrador Shelf; Grand Banks coverage
  in the available AI4Arctic subset is thin (one scene at 48.9–53.2°N).
- **Season:** February–July, matching the iceberg season. Behaviour outside that
  window is not characterised.
- **Pixel spacing:** the benchmark scenes are 80 m (AI4Arctic ready-to-train),
  not the 40 m of native EW GRD. Suppression thresholds expressed in pixels
  correspond to different ground distances between the two, and
  `min_target_pixels` in particular must be revisited when moving to 40 m data.

## 12. Not for navigation

Bathymetric data derived from CHS NONNA-100/NONNA-10 is licensed for research
and modelling only. **Not to be used for navigation.** No output of this system
is a navigational product or an ice hazard advisory.

---

## Summary table

| Area | Status |
|---|---|
| SAR calibration from AI4Arctic | Measured, physically verified |
| CFAR detection | Measured on 39 real S1 EW scenes |
| False-alarm suppression | Measured; ledger auditable |
| Detection density per 1000 km² | Measured (upper bound on FAR) |
| Precision / recall / mAP | **Not measured — no ground truth** |
| Absolute wind stratification | **Not available — relative only** |
| SAFE reader on real products | **Implemented, unvalidated** |
| Deep learning detector | **Not implemented** |
| Drift forecasting | **Scaffolded, not validated** |
| Live AIS correlation | **Interface only, no data** |
