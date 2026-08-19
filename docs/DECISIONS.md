# CryoLens Architecture Decision Records (ADRs)

This document records the foundational architectural decisions, scientific constraints, and trade-offs for the CryoLens project.

---

## ADR-001: Strict Radiometric SAR Preprocessing vs. Raw DN Processing

* **Date:** 2026-08-19
* **Status:** Accepted (Non-Negotiable)
* **Context:** Many computer vision applications treat Level-1 SAR Ground Range Detected (GRD) GeoTIFF Digital Numbers (DN) directly as optical-like pixel intensities. In SAR physics, DN values contain thermal noise variations, range-dependent radiometric gains, antenna pattern variations, and lack geometric terrain/ellipsoid correction.
* **Decision:** Every Sentinel-1 scene processed by CryoLens must pass through a full radiometric calibration pipeline before inference or feature extraction:
  1. Orbit Correction (POEORB precise orbit ephemerides, fallback to RESORB)
  2. Thermal Noise Removal (ESA metadata vectors + NERSC `s1denoise` Park et al. subswath tuning)
  3. Radiometric Calibration to Sigma Nought ($\sigma^0$)
  4. Geocoding / Ellipsoid Correction to `EPSG:3978`
* **Consequences:** Preprocessing takes additional compute and storage, but produces physically meaningful backscatter coefficients suitable for robust statistical CFAR and deep learning generalization across varying sea states and incidence angles.

---

## ADR-002: Multi-Channel Polarimetric and Geometric Input Stacking

* **Date:** 2026-08-19
* **Status:** Accepted
* **Context:** Icebergs exhibit volume scattering and high cross-polarization backscatter in HV, whereas open ocean backscatter drops significantly in HV, especially at medium-to-high incidence angles. However, wind-roughened sea clutter increases HH backscatter.
* **Decision:** ML models and feature extraction layers ingest a 4-channel polarimetric/geometric stack:
  * Band 1: $\sigma^0_{HH}$ in decibels ($\text{dB}$)
  * Band 2: $\sigma^0_{HV}$ in decibels ($\text{dB}$)
  * Band 3: Polarimetric ratio $\sigma^0_{HH}/\sigma^0_{HV}$ ($\text{dB difference} = \sigma^0_{HH\text{ (dB)}} - \sigma^0_{HV\text{ (dB)}}$)
  * Band 4: Local incidence angle $\theta_{inc}$ (degrees)
* **Consequences:** Retains full polarimetric contrast while providing the model with incidence-angle awareness to compensate for radar cross-section falloff from near-range to far-range.

---

## ADR-003: Project Coordinate Reference System (CRS) Selection

* **Date:** 2026-08-19
* **Status:** Accepted
* **Context:** The Grand Banks of Newfoundland and the NE Newfoundland Shelf span from $60.0^\circ\text{W}$ to $46.0^\circ\text{W}$ ($43.5^\circ\text{N}$ to $55.0^\circ\text{N}$). This area spans across three separate UTM zones:
  * UTM Zone 21N ($60^\circ\text{W}$ to $54^\circ\text{W}$)
  * UTM Zone 22N ($54^\circ\text{W}$ to $48^\circ\text{W}$)
  * UTM Zone 23N ($48^\circ\text{W}$ to $42^\circ\text{W}$, outer Flemish Cap)
* **Decision:** Use **`EPSG:3978` (NAD83 / Canada Atlas Lambert)** as the unified project CRS.
* **Rationale:** 
  * Lambert Conformal Conic (LCC) is a **conformal** projection (preserves local angles and shapes).
  * Preserving angular fidelity is critical for drift trajectory modeling, force-balance vector additions, and shape-ratio estimation of targets.
  * Avoids boundary discontinuities, tile seam distortion, and multi-CRS joins across the $54^\circ\text{W}$ and $48^\circ\text{W}$ meridians.
  * Matches the operational projection standard of the Canadian Ice Service (CIS).

---

## ADR-004: CFAR Detector as the Foundational Baseline (Linear Power Space)

* **Date:** 2026-08-19
* **Status:** Accepted
* **Context:** Deep learning models in SAR are frequently benchmarked against strawmen or tested on isolated chips without reporting the false-alarm rate across wide swaths of open ocean.
* **Decision:**
  * Implement classical Cell-Averaging CFAR (CA-CFAR) and K-distribution/Gamma CFAR first in Phase 2 before training any deep learning models.
  * **Critical Computation Rule:** CFAR statistics (mean, variance, guard band estimation) must be calculated on **linear power / intensity** ($\sigma^0$) where speckle follows multiplicative physical distributions. Decibel data is strictly converted to linear intensity on the fly for CFAR evaluation.
  * Evaluate all models on identical Receiver Operating Characteristic (ROC) curves with operational metric: **False Alarms per $1000\text{ km}^2$**.

---

## ADR-005: IIP Ground Truth Temporal Drift Semantics & Resolution Limits

* **Date:** 2026-08-19
* **Status:** Accepted
* **Context:** International Ice Patrol (IIP / NSIDC G00807) sightings are recorded by aerial and shipboard reconnaissance at specific timestamps and coded size categories. Icebergs drift at typical speeds of $0.1\text{ to }0.5\text{ m/s}$. A 6-hour temporal offset between sighting and satellite overpass equates to a $2.1\text{ to }10.8\text{ km}$ spatial displacement ($50\text{ to }270$ pixels on S1 EW $40\text{ m}$ GRD).
* **Decision:**
  * Never naively intersect IIP coordinates with SAR pixels to generate positive training labels.
  * Use IIP as a weak-supervision search prior: filter sightings to $\pm \Delta t$, buffer candidate areas by $r = v_{max} \cdot \Delta t + \text{position\_uncertainty}$, and mine CFAR detections within the buffer for human validation in Label Studio.
  * **Size Resolution Boundary:** Sentinel-1 EW GRD ($40\text{ m}$ pixel spacing, $\sim 90\text{ m}$ resolution) cannot reliably resolve IIP Growlers ($<5\text{ m}$) or Bergy Bits ($5\text{–}15\text{ m}$). Evaluation metrics must be stratified by IIP size class so undetectable sub-resolution targets do not distort recall metrics.

---

## ADR-006: Physics-First Drift Forecasting (OpenDrift openberg + CHS NONNA-100)

* **Date:** 2026-08-19
* **Status:** Accepted
* **Context:** Pure machine learning drift predictors can overfit or learn circular approximations of coarse reanalysis currents. Furthermore, bathymetric grounding on the shallow Grand Banks ($50\text{–}100\text{ m}$) is a primary physical halt condition.
* **Decision:**
  * The baseline drift model uses OpenDrift's `openberg` physical model with dynamic ocean currents (CMEMS), atmospheric wind forcing (ERA5/NWP), and multi-layer iceberg keel geometry.
  * Keel grounding checks are performed against high-resolution CHS NONNA-100 bathymetry.
  * ML (XGBoost) is applied strictly as a residual correction to the physical model output.
