# CryoLens 🧊🛰️

**Autonomous SAR Iceberg Detection, Validation, and Drift Forecasting for the Grand Banks & NE Newfoundland Shelf.**

Targeting Maritime Domain Awareness (MDA), polarimetric radar signal processing, and operational MLOps standards.

---

## 1. Overview & Core Mission

The Grand Banks and the Northeast Newfoundland Shelf ("Iceberg Alley") present one of the most demanding operational environments in maritime remote sensing: persistent cloud cover, high sea states, dynamic sea ice margins, and intense vessel traffic around offshore oil fields (Hibernia, Terra Nova, White Rose) and shipping lanes.

CryoLens provides an end-to-end pipeline processing Sentinel-1 Extra Wide (EW) Swath SAR imagery to detect, classify, human-validate, and drift-forecast icebergs against ocean current and atmospheric forcing.

```
                  ┌─────────────────────────────────────────┐
                  │   Copernicus Sentinel-1 (EW GRD HH+HV)   │
                  └────────────────────┬────────────────────┘
                                       │
                      [SAR Radiometric Preprocessing]
                      • Precise Orbit Ephemerides (POEORB)
                      • Thermal Noise Removal (s1denoise)
                      • Radiometric Calibration to σ⁰
                      • Ellipsoid Correction (EPSG:3978)
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │    Polarimetric & Geometric Stack     │
                   │  [σ⁰_HH, σ⁰_HV, σ⁰_HH/σ⁰_HV, θ_inc]   │
                   └───────────────────┬───────────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    ▼                                     ▼
        ┌───────────────────────┐             ┌───────────────────────┐
        │  Classical Baseline   │             │   Deep Learning (P2)  │
        │   CA / K-dist CFAR    │             │   Custom YOLOv8 / CNN │
        └───────────┬───────────┘             └───────────┬───────────┘
                    │                                     │
                    └──────────────────┬──────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │    Unified ROC / FAR Benchmark Suite    │
                  │   (Stratified by Ice & Wind Regimes)    │
                  └────────────────────┬────────────────────┘
                                       │
                                       ▼
                  ┌─────────────────────────────────────────┐
                  │        PostGIS Detection Registry       │
                  └──────────────┬───────────────────┬──────┘
                                 │                   │
               [Analyst Feedback Loop]               ▼
            QGIS PyQt5 Analyst Validation     [OpenDrift openberg]
                         │                    • Multi-layer hydrodynamic drag
                         ▼                    • NONNA-100 keel grounding
             DVC-Versioned Retraining         • Residual ML displacement model
```

---

## 2. Key Architectural Differentiators

Unlike generic object detection pipelines, CryoLens implements the physical and statistical rigor demanded by radar oceanography:

1. **True SAR Radiometric Processing:** Sentinel-1 Digital Numbers (DN) are strictly calibrated through orbit correction $\rightarrow$ thermal noise removal $\rightarrow$ radiometric calibration to $\sigma^0$ $\rightarrow$ geocoding. Raw DN values are never fed directly to models.
2. **Subswath Scalloping Correction:** Extra Wide (EW) cross-pol (HV) imagery suffers from Noise Equivalent Sigma Zero (NESZ) scalloping. CryoLens integrates the NERSC `s1denoise` algorithm (Park et al.) alongside SNAP to prevent false-alarm stripes.
3. **Polarimetric Feature Tensor:** 4-band input stack $[\sigma^0_{HH}, \sigma^0_{HV}, \text{Ratio}_{HH/HV}, \theta_{inc}]$.
4. **CFAR Baseline First:** Statistical CFAR (Cell-Averaging and K-distribution) on linear intensity is tuned and evaluated on the exact same benchmark curves (False Alarms per $1000\text{ km}^2$) before deep learning models.
5. **Sea Ice as a First-Class Regime:** Metrics are explicitly stratified across open water vs. sea ice regimes (CIS SIGRID-3 / AI4Arctic) and wind clutter speeds (ERA5).
6. **Physics-First Drift Forecasting:** OpenDrift (`openberg`) multi-layer drag physics with CHS NONNA-100 bathymetry grounding detection + residual XGBoost corrections.
7. **Analyst-in-the-Loop Workflow:** QGIS validation plugin writing directly to PostGIS, feeding analyst corrections back into DVC-versioned training manifests.

---

## 3. Honest Data Constraints & AIS Notice

* **Historical Point-Level AIS Availability:** High-resolution historical vessel AIS tracks over the Grand Banks are proprietary and not freely available in public datasets (unlike US waters in NOAA Marine Cadastre). Vessel training labels in CryoLens are sourced from the verified AIS-matched **xView3-SAR** dataset. Live correlation is architected as an extensible interface with clear hooks for commercial AIS feeds (Spire / exactEarth / MarineTraffic).
* **IIP Ground Truth Semantics:** International Ice Patrol (IIP / NSIDC G00807) sightings record visual sightings with inherent temporal offsets ($\pm \Delta t$). Because icebergs drift at $0.1\text{–}0.5\text{ m/s}$, a 6-hour gap corresponds to $2\text{–}10\text{ km}$ of drift. IIP is used as a weak-supervision search prior, never naively intersected with SAR pixels.

---

## 4. Quickstart & Development

### Prerequisites
* Python 3.11+
* `uv` package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `winget install astral-sh.uv`)
* Docker & Docker Compose (for local PostGIS 16 database)

### Setup
```bash
# 1. Clone repository
git clone https://github.com/your-org/CryoLens.git
cd CryoLens

# 2. Set up virtual environment and install dependencies
make dev

# 3. Configure credentials
cp .env.example .env
# Edit .env with your credentials (see .env.example for descriptions)

# 4. Start PostGIS 16 database
make db-up

# 5. Run test suite and linters
make test
make lint
```

---

## 5. Repository Structure

```
CryoLens/
├── configs/             # AOI GeoJSON, project.yaml, SNAP GPT graphs
├── data/                # raw/, interim/, processed/ (strictly gitignored)
├── docs/                # DECISIONS.md (Architecture Decision Records)
├── src/cryolens/
│   ├── config/          # Type-safe pydantic-settings & YAML loader
│   ├── ingest/          # CDSE STAC, ASF DAAC, Planetary Computer
│   ├── preprocess/      # SNAP GPT graphs, s1denoise, COG stack builder
│   ├── detect/          # CA/K-dist CFAR, CNN classifier, YOLOv8
│   ├── eval/            # Benchmark harness (ROC, FAR/1000km2, stratified metrics)
│   ├── geo/             # Affine georeferencing, vectorization, CRS transforms
│   ├── drift/           # OpenDrift openberg, CHS NONNA-100 grounding, XGBoost
│   ├── api/             # FastAPI REST endpoints
│   └── db/              # PostGIS models, migrations, and sessions
├── tests/               # Unit and integration test suite
├── docker-compose.yml   # PostGIS 16-3.4 service
├── Makefile             # Development automation targets
└── pyproject.toml       # Pinned dependencies & tooling configs
```

---

## 6. License & Non-Navigational Notice

* **Software:** MIT License
* **Canadian Hydrographic Service (CHS) Notice:** Bathymetric data derived from CHS NONNA-100/NONNA-10 is for research and modeling purposes only. **Not to be used for navigation.**
