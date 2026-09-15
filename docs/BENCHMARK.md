# Audited candidate-density benchmark

**Research result. Precision, recall and empirical false-positive rate are not measured.**

This report supersedes the withdrawn historical 39-scene headline. Its counts cannot be compared as detector improvements: the normalization, CFAR model, geographic mask and area denominator changed.

Detector: **Gamma-CFAR**. Region: **NL shelf study polygon**.

| Quantity | Value |
|---|---:|
| Processed scenes | 25 |
| Cumulative analyzed area (km², nominal spacing) | 1,670,147.0 |
| Raw connected candidates | 6,022 |
| Retained unverified candidates | 311 |
| Raw candidates / 1,000 km² | 3.606 |
| Retained candidates / 1,000 km² | 0.186 |

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
    "eligible_scenes": 28,
    "selected_scenes": 28,
    "processed_scenes": 25,
    "failed_scenes": [],
    "skipped_scenes": [
      {
        "scene_id": "20190406T102029_cis_prep.nc",
        "reason": "No eligible water after AOI, quality and training-support masks",
        "mask_breakdown": {
          "invalid_or_nodata": 0.4000474571849962,
          "land_and_coastal_buffer": 0.07621915978375224,
          "swath_border": 0.027935066950018978,
          "subswath_seams": 1.9548465978560666e-05,
          "sea_ice_or_unknown": 0.495778767615254,
          "outside_nl_study_area": 0.0
        }
      },
      {
        "scene_id": "20200217T102731_cis_prep.nc",
        "reason": "No eligible water after AOI, quality and training-support masks",
        "mask_breakdown": {
          "invalid_or_nodata": 0.13291702672126973,
          "land_and_coastal_buffer": 0.010079851612776258,
          "swath_border": 0.021896128048835034,
          "sea_ice_or_unknown": 0.8351069936171189,
          "outside_nl_study_area": 0.0
        }
      },
      {
        "scene_id": "20200319T101935_cis_prep.nc",
        "reason": "No eligible water after AOI, quality and training-support masks",
        "mask_breakdown": {
          "invalid_or_nodata": 0.16992405154052737,
          "land_and_coastal_buffer": 0.009392650694104502,
          "swath_border": 0.018860511854515802,
          "subswath_seams": 0.0015912429387688787,
          "sea_ice_or_unknown": 0.8002315429720834,
          "outside_nl_study_area": 0.0
        }
      }
    ]
  }
]
```

## Sea-ice strata

| Stratum | Scenes | Area km² | Candidates | / 1,000 km² | Interpretable |
|---|---:|---:|---:|---:|---|
| ice_affected | 19 | 1,065,212.6 | 223 | 0.209 | yes |
| open_water | 6 | 604,934.4 | 88 | 0.145 | yes |

## Wind strata

| Stratum | Scenes | Area km² | Candidates | / 1,000 km² | Interpretable |
|---|---:|---:|---:|---:|---|
| high | 9 | 524,616.7 | 57 | 0.109 | yes |
| low | 9 | 652,618.1 | 130 | 0.199 | yes |
| moderate | 7 | 492,912.2 | 124 | 0.252 | yes |

## Suppression ledger

| Stage | Removed | Remaining |
|---|---:|---:|
| min_size | 5,709 | 313 |
| max_size | 0 | 313 |
| aspect_ratio | 0 | 313 |
| min_peak_hv | 0 | 313 |
| copol_dominance | 1 | 312 |
| clutter_contrast | 1 | 311 |

## Reproduction and evidence

Machine-readable evidence with per-scene assumptions, source checksums and suppression ledgers: [audited_results.json](benchmarks/audited_results.json). See [DATA.md](DATA.md), [AUDIT.md](AUDIT.md) and [LIMITATIONS.md](LIMITATIONS.md).

No certified C-CORE operating point, MANICE confidence code or navigational hazard assessment is established by this experiment.
