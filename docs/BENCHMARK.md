# Audited candidate-density benchmark

**Research result. Precision, recall and empirical false-positive rate are not measured.**

This report supersedes the withdrawn historical 39-scene headline. Its counts cannot be compared as detector improvements: the normalization, CFAR model, geographic mask and area denominator changed.

Detector: **Gamma-CFAR**. Region: **NL shelf study polygon**.

| Quantity | Value |
|---|---:|
| Processed scenes | 3 |
| Cumulative analyzed area (km², nominal spacing) | 325,238.6 |
| Raw connected candidates | 1,361 |
| Retained unverified candidates | 35 |
| Raw candidates / 1,000 km² | 4.185 |
| Retained candidates / 1,000 km² | 0.108 |

Candidate density counts all retained returns regardless of their true identity. Fewer returns can include missed real targets. Repeated observations accumulate coverage; this is not unique ocean area. Masked water was not surveyed by the detector.

## Methodology

```json
{
  "metric": "unverified_candidate_density_per_1000km2",
  "precision_recall_measured": false,
  "design_pfa_is_measured_far": false,
  "classification": "unclassified; radiometric heuristic stored separately",
  "confidence_kind": "uncalibrated_heuristic_score",
  "aoi": "configs/aoi.geojson newfoundland_labrador_marine; per-pixel centre clipping",
  "area": "eligible pixel count times nominal spacing squared; exposure sum, not unique ocean area",
  "suppression": {
    "coastal_buffer_zones": 2,
    "border_exclusion_px": 64,
    "max_sic_class_for_open_water": 1,
    "exclude_sea_ice": true,
    "seam_detection_enabled": true,
    "seam_exclusion_px": 8,
    "seam_gradient_sigma": 6.0,
    "max_seam_groups": 6,
    "min_target_pixels": 4,
    "max_target_pixels": 20000,
    "max_aspect_ratio": 8.0,
    "min_contrast_db": 3.0,
    "min_peak_hv_db": -30.0,
    "max_hh_hv_ratio_db": 18.0
  },
  "source_scaling": "See per-scene assumptions and source_sha256"
}
```

## Run accounting

```json
[
  {
    "detector": "gamma",
    "pfa": 1e-06,
    "eligible_scenes": 25,
    "selected_scenes": 3,
    "processed_scenes": 3,
    "failed_scenes": [],
    "skipped_scenes": []
  }
]
```

## Sea-ice strata

| Stratum | Scenes | Area km² | Candidates | / 1,000 km² |
|---|---:|---:|---:|---:|
| ice_affected | 2 | 191,675.4 | 7 | 0.037 |
| open_water | 1 | 133,563.2 | 28 | 0.210 |

## Wind strata

| Stratum | Scenes | Area km² | Candidates | / 1,000 km² |
|---|---:|---:|---:|---:|
| high | 1 | 113,875.4 | 6 | 0.053 |
| low | 1 | 133,563.2 | 28 | 0.210 |
| moderate | 1 | 77,800.0 | 1 | 0.013 |

## Suppression ledger

| Stage | Removed | Remaining |
|---|---:|---:|
| min_size | 1,326 | 35 |
| max_size | 0 | 35 |
| aspect_ratio | 0 | 35 |
| min_peak_hv | 0 | 35 |
| copol_dominance | 0 | 35 |
| clutter_contrast | 0 | 35 |

## Reproduction and evidence

Machine-readable evidence with per-scene assumptions, source checksums and suppression ledgers: [audited_results.json](benchmarks/audited_results.json). See [DATA.md](DATA.md), [AUDIT.md](AUDIT.md) and [LIMITATIONS.md](LIMITATIONS.md).

No certified C-CORE operating point, MANICE confidence code or navigational hazard assessment is established by this experiment.

## Reproduce this small evaluation

The first three geographically eligible labeled training scenes are selected
in filename order, independently of detection counts:

```text
uv run --frozen python -m cryolens.eval --data-root data/raw/ai4arctic/train --limit 3 --pfa 1e-6 --open-water-only --output-dir data/processed/benchmarks-audit
```

This is a three-acquisition engineering check, not validation for all of NL or
all seasons. Scene-level ice strata can be "ice_affected" even though only the
charted open-water pixels within those scenes were analyzed. Wind bins are
relative cohort terciles of restored ERA5 speed, not operational sea-state
classes; three scenes cannot support a wind-response conclusion.

The earlier challenge-test attempt had no eligible open-water coverage:
[withheld-scene manifest](benchmarks/withheld_scene_manifest.json). Those skipped
scenes are not interpreted as zero false positives.
