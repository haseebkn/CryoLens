# CryoLens 🧊🛰️

**Sentinel-1 SAR iceberg detection for the Newfoundland & Labrador shelf, measured against an operational false-alarm budget.**

---

## The result

Running a Gamma/K-distribution CFAR detector plus a multi-stage suppression
chain over **PENDING_N_SCENES real Sentinel-1 Extra Wide swath scenes**
(PENDING_AREA km² of analysed water) across the Labrador Shelf and Grand Banks:

| Metric | Value |
|---|---|
| Raw CFAR candidates per 1000 km² | **PENDING_RAW** |
| After false-alarm suppression | **PENDING_FINAL** |
| Suppression factor | **PENDING_FACTOR** |

Full table, per-stage ledger, ice/wind stratification and the Pfa operating-point
curve: **[docs/BENCHMARK.md](docs/BENCHMARK.md)**.

### What that number is, and is not

It is **detection density per 1000 km² of analysed water**. Over open water away
from land and ice, genuine icebergs are sparse at EW resolution, so this figure
is dominated by false alarms and serves as a defensible **upper bound on the
false-alarm rate**.

It is **not** precision, recall, or mAP. No verified iceberg positions exist for
these scenes: AI4Arctic supplies ice charts, not iceberg point truth; IIP
sightings cannot be intersected with SAR pixels (a 6-hour offset is 2–11 km of
drift); xView3-SAR annotates vessels. Reporting precision without positives
would be fabrication.

**[docs/LIMITATIONS.md](docs/LIMITATIONS.md) states in full what this system does
not do.** Read it before relying on any number here.

---

## Why this is not another YOLO-on-satellite-images project

Four things, each of which changes the result rather than the presentation:

**1. Digital numbers are not backscatter.** Every scene passes orbit correction →
thermal noise removal → radiometric calibration to σ⁰ → geocoding before any
model sees it. `preprocess/safe_reader.py` implements calibration from the ESA
product annotations directly: σ⁰ = DN²/A²σ, with the noise LUT subtracted in
**linear power** (the noise floor is additive in power, not in decibels).

**2. Dual-pol, not single-channel.** The feature stack is
[σ⁰_HH, σ⁰_HV, HH/HV ratio, θ_inc]. Icebergs volume-scatter and show strong
cross-pol return; open water collapses in HV. Detection runs on HV, and the
co-pol ratio vetoes specular sea-surface returns.

**3. CFAR is the measured baseline, not a strawman.** All statistics are computed
in linear power via 2D integral images — O(1) per pixel with exact guard-band
exclusion. The Gamma CFAR uses a method-of-moments shape estimate for
heavy-tailed clutter at higher sea states.

**4. The false-alarm budget is auditable.** Suppression is an ordered chain and
every stage records what it removed:

```
stage              removed  remaining
min_size              3955        128
max_size                 0        128
aspect_ratio             0        128
min_peak_hv              1        127
copol_dominance          2        125
clutter_contrast         0        125
```

That ledger is honest about something a single aggregate number would hide: one
stage does most of the work, because CFAR speckle hits are predominantly
isolated single pixels while real targets form clusters. The recall cost of that
threshold is **not measured** — see LIMITATIONS §9.

---

## Architecture

```
        Copernicus / ASF  ──►  SAFE product (EW GRD, HH+HV)
                                       │
                    ┌──────────────────▼──────────────────┐
                    │  safe_reader.py                     │
                    │  orbit · thermal noise · σ⁰ · geoloc │
                    └──────────────────┬──────────────────┘
                                       │  4-band COG, EPSG:3978
                    ┌──────────────────▼──────────────────┐
                    │  build_analysis_mask()              │
                    │  GSHHG land + coastal buffer        │
                    │  swath borders · subswath seams     │
                    │  sea ice (flagged, not discarded)   │
                    └──────────────────┬──────────────────┘
                                       │  where CFAR may look
                    ┌──────────────────▼──────────────────┐
                    │  CA-CFAR  /  Gamma-CFAR             │
                    │  linear power · integral images     │
                    └──────────────────┬──────────────────┘
                                       │  pixel hits
                    ┌──────────────────▼──────────────────┐
                    │  vectorise → filter_targets()       │
                    │  size · aspect · contrast · co-pol  │
                    │  cross-tile NMS in projected coords │
                    └──────────────────┬──────────────────┘
                                       │
                          PostGIS  ──►  FastAPI  ──►  Leaflet
                                       │
                          eval/benchmark.py  ──►  BENCHMARK.md
```

---

## Area of interest

Newfoundland and Labrador marine area: **64.5°W–44.0°W, 42.5°N–60.5°N**. That
spans Iceberg Alley end to end — the Labrador Shelf transit corridor down to the
Tail of the Grand Bank and Flemish Cap, taking in the offshore production fields
(Hibernia, Terra Nova, White Rose) and the transatlantic lanes.

Scene selection requires the scene **centre** inside the AOI, not merely an
overlap. A Sentinel-1 EW swath is ~400 km across, so a scene can clip the corner
of the box while lying almost entirely in Ungava Bay.

Project CRS is **EPSG:3978** (NAD83 / Canada Atlas Lambert). Conformal, so local
angles are preserved for drift vectors and target shape ratios, and it avoids
the UTM zone seams at 54°W and 48°W that cut straight through the AOI.

---

## Quickstart

```bash
make dev              # editable install with dev dependencies
make fetch-shorelines # GSHHG coastlines, 150 MB, no credentials needed
make db-up            # PostGIS 16
make test             # 106 tests
make lint             # ruff + mypy
```

To reproduce the benchmark you need the AI4Arctic ready-to-train scenes (see
[Data](#data)), then:

```bash
make scene-index
make benchmark SWEEP=1
```

To run the API and dashboard:

```bash
make api   # http://localhost:8000  (docs at /docs)
```

---

## Data

| Dataset | Role | Access |
|---|---|---|
| **AI4Arctic Sea Ice Challenge** (ready-to-train) | Real S1 EW HH+HV with co-registered CIS ice charts, ERA5 forcing and land-distance zonation. The measured benchmark runs on this. | Direct download, DOI `10.11583/DTU.c.6244065` |
| **GSHHG** full-resolution shorelines | Land masking, 6,404 polygons over the AOI | `make fetch-shorelines` |
| **Copernicus / ASF** Sentinel-1 EW GRD | The live path via `safe_reader.py` | **Credentials required** |
| **NSIDC G00807** (IIP sightings) | Weak-supervision search prior only, never labels | NASA Earthdata login |
| **xView3-SAR**, **Statoil/C-CORE Kaggle** | Detector and classifier training | Terms acceptance |

Note on the AI4Arctic distribution: pixel values are **standardised, not
physical**. For the two SAR channels the packagers preserved the
pre-normalisation extremes in the `min`/`max` variable attributes, so σ⁰ in
decibels is recovered exactly by inverting the linear map. Recovered open-water
HV medians land near **−33 dB**, which is the correct regime and serves as the
physical sanity check. The ERA5 winds carry no such attributes, so wind
stratification is reported in **relative terciles**, not m/s (LIMITATIONS §3).

---

## Repository layout

```
src/cryolens/
├── config/       type-safe pydantic-settings + YAML loader
├── ingest/       CDSE STAC, ASF, Planetary Computer, IIP, LRU cache
├── preprocess/   safe_reader (σ⁰ calibration), s1denoise, masks, COG stack
├── data/         AI4Arctic reader, unit restoration, scene indexing
├── detect/       CFAR (CA + Gamma), suppression chain, scene runner
├── geo/          vectorisation, affine and tie-point georeferencing
├── eval/         benchmark harness, IIP spatiotemporal correlation
├── drift/        OpenDrift openberg scaffold (see LIMITATIONS §8)
├── api/          FastAPI + GeoJSON
└── db/           PostGIS models, repositories, Alembic migrations
```

---

## Status

| Component | State |
|---|---|
| SAR calibration from AI4Arctic | Measured, physically verified |
| CFAR detection + suppression | Measured on real S1 EW scenes |
| Detection density benchmark | Measured, stratified |
| SAFE reader | Implemented, unit-tested, **unvalidated on a real product** |
| Deep learning detector | **Not implemented** — interface raises |
| Drift forecasting | **Scaffolded, not validated** |
| QGIS analyst plugin | **Not started** |
| Live AIS correlation | **Interface only** — no free point-level AIS for this region |

---

## Design decisions

Twelve ADRs in [docs/DECISIONS.md](docs/DECISIONS.md) record the reasoning,
including the ones that constrain what this project is allowed to claim:

- **ADR-004** — CFAR in linear power space, as the reference baseline
- **ADR-005** — why IIP sightings are a search prior and never a label
- **ADR-009** — suppression as an auditable multi-stage chain
- **ADR-011** — detection density per 1000 km² as the reportable metric
- **ADR-012** — refusing to fabricate data in place of unimplemented components

ADR-012 is the one worth reading. Three components previously returned synthetic
data so the pipeline would appear to work end to end: the orchestrator generated
random arrays with hardcoded bright rectangles instead of reading the downloaded
product, the YOLO detector returned a fixed point regardless of input, and the
chip extractor wrote zero-byte files. All three now fail loudly instead. The
honest surface area of this project is smaller than the fabricated one was, and
it is measured.

---

## License & notices

* **Software:** MIT
* **CHS notice:** Bathymetry derived from CHS NONNA-100/NONNA-10 is for research
  and modelling only. **Not to be used for navigation.**
* No output of this system is a navigational product or an ice hazard advisory.
