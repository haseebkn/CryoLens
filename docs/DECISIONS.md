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

---

## ADR-007: Sentinel-1 EW Subswath Noise Removal — SNAP vs. s1denoise

* **Date:** 2026-08-19
* **Status:** Accepted (Recommendation: NERSC `s1denoise`)
* **Context:** Sentinel-1 Extra Wide (EW) swath SAR imagery consists of 5 subswaths (EW1 to EW5). Thermal noise floor variations (NESZ scalloping between $-28\text{ dB}$ and $-24\text{ dB}$) in the cross-polarization channel (HV) exceed calm ocean backscatter ($<-32\text{ dB}$). Standard ESA SNAP `ThermalNoiseRemoval` operator removes standard metadata noise vectors but frequently leaves residual inter-subswath boundary steps and scalloping streaks, triggering false-alarm stripes along range seams in CFAR and object detection.
* **Evaluation & Decision:**
  * **ESA SNAP Standard TNR:** Suitable for high-backscatter land surfaces, but insufficient for low-clutter maritime open ocean.
  * **NERSC `s1denoise` (Park et al. 2018, Korosov et al. 2022):** Dynamically estimates inter-subswath scaling factors $k_i$ across overlap regions and subtracts scaled noise floors in linear power space without negative clipping.
  * **Recommendation:** Use `s1denoise` as the primary thermal denoising engine across all operational detection pipelines. SNAP remains available as a secondary reference in `configs/snap/s1_ew_grd_preprocessing.xml`.

---

## ADR-008: Statistical CFAR Detection Architecture, Clutter Distribution Models, and 2D Integral Images

* **Date:** 2026-08-19
* **Status:** Accepted
* **Context:** Maritime SAR target detection requires robust statistical thresholds to maintain a constant false-alarm probability ($P_{fa}$) under varying sea states and wind clutter. Decibel SAR data violates the multiplicative Rayleigh/exponential speckle assumption, necessitating all CFAR operations in linear power intensity space ($\sigma^0$). Furthermore, 2D sliding-window filtering with spatial guard bands is computationally intensive on large SAR rasters ($8000 \times 8000$ pixels).
* **Decision:**
  1. **Linear Power Execution:** All CFAR statistics (mean, variance, quantile inversion) are strictly computed on linear intensity $I = 10^{\sigma^0_{\text{dB}} / 10}$.
  2. **CA-CFAR Formulation:** For a 2D rectangular window with background half-width $W_b$ and guard half-width $W_g$, the valid training cell count is $N_{train} = (2W_b + 1)^2 - (2W_g + 1)^2 - 1$. The threshold multiplier for iid exponential speckle is $\alpha = N_{train}(P_{fa}^{-1/N_{train}} - 1)$, producing threshold $T = \alpha \cdot \hat{\mu}$.
  3. **Gamma / K-Distribution CFAR for Heterogeneous Clutter:** For sea states $\ge 3$, sea clutter exhibits heavy-tailed non-Gaussian texture. We fit a two-parameter Gamma distribution via the Method of Moments (MoM) estimator: $\hat{\nu} = \hat{\mu}^2 / (\hat{\sigma}^2 - \hat{\mu}^2/N_{train})$, bounded to $[0.1, 50.0]$. The detection threshold is inverted via the upper quantile: `scipy.stats.gamma.ppf(1 - P_fa, a=nu, scale=mu/nu)`.
  4. **2D Integral Image Optimization:** Rather than using spatial convolutions or `scipy.ndimage.uniform_filter` (which cannot exclude inner guard bands), we implement 2D prefix-sum integral images for intensity $I$, intensity squared $I^2$, and valid pixel masks. This enables exact $O(1)$ rectangle queries per pixel, achieving $>10\times$ speedup over iterative sliding windows.



---

## ADR-009: False-Alarm Suppression as an Auditable Multi-Stage Chain

* **Date:** 2026-09-03
* **Status:** Accepted
* **Context:** A CFAR detector is tuned to a *pixel-wise* probability of false alarm. That guarantee says nothing about the absolute number of false detections on a full Sentinel-1 EW swath. A $5000 \times 5400$ raster at $P_{fa} = 10^{-5}$ yields on the order of 270 spurious pixel hits over ideal homogeneous clutter, before any structured artefact. Measured on real Labrador Shelf scenes, raw CFAR output ran at roughly $26$ candidates per $1000\text{ km}^2$ — far above an operationally usable rate.
* **Decision:** Suppression is implemented as an explicit ordered chain in `detect/filters.py`, split into two groups, with every stage recording how many candidates it removed.
  1. **Pre-detection masking** (`build_analysis_mask`) decides where CFAR may look *and* which pixels may contribute to clutter statistics: land plus a coastal buffer, swath borders, detected subswath seams, and optionally sea ice.
  2. **Post-detection gating** (`filter_targets`) rejects candidates on connected-component size, aspect ratio, peak cross-pol backscatter, co-pol dominance, and contrast against the local clutter floor.
  3. **Cross-tile deduplication** (`deduplicate_across_tiles`) resolves the same physical target detected in overlapping tiles, in projected coordinates rather than pixel space.
* **Rationale for the ledger:** A single aggregate suppression number is unfalsifiable. Recording per-stage removals makes the false-alarm budget auditable and exposes — rather than conceals — that one stage (minimum component size) does most of the work, because CFAR speckle hits are predominantly isolated single pixels while genuine targets form clusters.
* **Consequences:** Measured suppression is roughly $30\times$, taking detection density from about $26$ to below $1$ per $1000\text{ km}^2$. The recall cost is **not measured**, because no iceberg ground truth exists for these scenes; raising the minimum size threshold necessarily discards small icebergs. This trade-off is stated in `docs/LIMITATIONS.md` rather than buried.

---

## ADR-010: Land Masking from GSHHG Shorelines, Not Hand-Drawn Polygons

* **Date:** 2026-09-03
* **Status:** Accepted
* **Context:** Coastal returns are the largest single source of false alarms in maritime SAR detection. An earlier implementation approximated Newfoundland with two hand-drawn polygons totalling eleven vertices, and omitted Labrador entirely. Newfoundland and Labrador have an intricate coast with thousands of islands, fjords and skerries; a coarse outline leaves bright land inside the analysis mask.
* **Decision:** Use GSHHG (Global Self-consistent, Hierarchical, High-resolution Geography) full-resolution level-1 polygons, clipped once to the AOI and cached as a GeoPackage. Clipping the NL marine area yields **6,404 shoreline polygons**. Apply a configurable seaward dilation (default $500\text{ m}$) before rasterisation.
* **Rationale for the buffer:** Masking only the land polygon is insufficient. Geolocation error, radar layover, and the CFAR background window all reach beyond the coastline; a target whose training window overlaps land gets an inflated clutter mean and is suppressed, while shoreline pixels themselves generate detections.
* **Consequences:** A one-time 150 MB download (`make fetch-shorelines`), and a first-run clipping pass. Both are cached. Land masking is now driven by a real shoreline product rather than by an approximation whose error was invisible.

---

## ADR-011: Detection Density per 1000 km² as the Reportable Metric

* **Date:** 2026-09-03
* **Status:** Accepted
* **Context:** ADR-004 committed to evaluating detectors on false alarms per $1000\text{ km}^2$. Executing that commitment requires verified positives to separate true from false detections. No freely available dataset provides iceberg positions co-registered to the Sentinel-1 acquisitions analysed here: AI4Arctic supplies ice charts (polygons describing sea ice, not icebergs), IIP supplies sightings that cannot be intersected with SAR pixels (ADR-005), and xView3-SAR supplies vessel annotations.
* **Decision:** Report **detection density per 1000 km² of analysed water**, stratified by sea ice regime and relative wind regime, and state explicitly that over open water away from land and ice this is an **upper bound on the false-alarm rate**, not an estimate of it. Do not report precision, recall, F1, or mAP.
* **Rationale:** Over open water at EW resolution, genuine icebergs are sparse, so detection density there is dominated by false alarms and is a defensible bound. Reporting precision without positives would be fabrication; reporting nothing would forfeit the one operational metric that *can* be measured rigorously.
* **Area normalisation:** Every rate is divided by the water area actually examined, after masking. False alarms per *scene* is meaningless when swath coverage, land fraction, and masking differ between acquisitions.
* **Consequences:** The headline number is honest but weaker than a full ROC curve. Recovering precision and recall requires the analyst validation loop (Phase 6) to generate verified labels, or a commercial iceberg-observation feed.

---

## ADR-012: Refusing to Fabricate Data in Place of Unimplemented Components

* **Date:** 2026-09-03
* **Status:** Accepted (Non-Negotiable)
* **Context:** An audit of the pipeline found three components that returned synthetic data so that the end-to-end flow would appear to work: `pipeline.py` generated random exponential arrays with hardcoded bright rectangles instead of reading the downloaded SAFE product; `detect/yolo.py` returned a fixed detection at $-52.0^\circ, 47.0^\circ$ regardless of input; `detect/dataset.py` wrote zero-byte placeholder chips. Each made a benchmark or a demonstration look successful while measuring nothing.
* **Decision:** A component that cannot do its job **fails loudly**. Specifically:
  * `pipeline.py` now reads real measurement rasters and calibration annotations through `preprocess/safe_reader.py`, and raises if the product cannot be calibrated. There is no synthetic fallback.
  * `detect/yolo.py` raises `NotImplementedError` with the prerequisites stated, rather than emitting a placeholder detection.
  * `detect/dataset.py` requires a `band_loader` and refuses to write placeholder chips without a pixel source.
  * `preprocess/masks.py` logs a warning when it falls back to an assumed uniform ice field, so an absent ice product can never masquerade as measured ice cover.
* **Rationale:** A detection produced from fabricated backscatter is worse than no detection, because it is indistinguishable from a real one downstream and silently corrupts any metric computed over it. For a system whose entire value proposition is a credible false-alarm rate, this is disqualifying.
* **Consequences:** Fewer components "work" end to end without credentials or data. `make train-yolo` now exits non-zero with an explanation. This is the intended behaviour: the honest surface area of the project is smaller than the fabricated one was, and is measured.
