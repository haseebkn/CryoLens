# Detection Benchmark

**Detector:** Gamma-CFAR · **Region:** Newfoundland & Labrador shelf  
**Scenes:** 39 Sentinel-1 EW GRDM acquisitions · **Analysed water:** 3,888,568 km²

---

## Headline

| Metric | Value |
|---|---:|
| Raw CFAR candidates per 1000 km² | 50.47 |
| **After suppression, per 1000 km²** | **1.13** |
| Suppression factor | 44.5× |
| Total detections retained | 4,405 |
| Total raw candidates | 196,236 |

### What this number is

**Detection density per 1000 km² of analysed water.** Over open water away
from land and ice, genuine icebergs are sparse at Sentinel-1 EW resolution,
so this figure is dominated by false alarms and is reported as an **upper
bound on the false-alarm rate**.

It is **not** precision, recall, or mAP. No verified iceberg positions exist
for these scenes — see [LIMITATIONS.md](LIMITATIONS.md) §1. Every rate is
normalised by the water area actually examined after masking, because false
alarms *per scene* is meaningless when swath coverage and land fraction differ
between acquisitions.

---

## Stratified results

### By sea ice regime

| stratum | scenes | water area (km²) | detections | per 1000 km² | suppression |
|---|---:|---:|---:|---:|---:|
| ice_affected | 30 | 2,823,745 | 3,811 | 1.35 | 43.2× |
| open_water | 6 | 785,487 | 218 | 0.28 | 105.2× |
| unknown | 3 | 279,337 | 376 | 1.35 | 23.5× |

`unknown` covers the AI4Arctic challenge test scenes, whose ice charts are
withheld. They are reported separately rather than folded into open water,
which would understate detection density over ice.

### By relative wind regime

| stratum | scenes | water area (km²) | detections | per 1000 km² | suppression |
|---|---:|---:|---:|---:|---:|
| high | 13 | 1,424,679 | 1,856 | 1.30 | 47.5× |
| low | 13 | 1,312,436 | 1,278 | 0.97 | 47.8× |
| moderate | 13 | 1,151,454 | 1,271 | 1.10 | 37.0× |

Wind bins are **terciles across this scene cohort**, not absolute m/s, and
not within-scene quantiles. The ERA5 fields in this distribution are
standardised with no recorded extremes, so metres per second is
unrecoverable (LIMITATIONS §3); the per-scene median still orders scenes
correctly, so the split is between scenes rather than inside one.
`unknown` marks scenes carrying no wind field, which are excluded from the
tercile fit.

---

## Suppression ledger

Where the candidate budget went, summed across every scene:

| stage | removed | remaining | % of raw |
|---|---:|---:|---:|
| `min_size` | 190,863 | 5,373 | 97.3% |
| `max_size` | 0 | 5,373 | 0.0% |
| `aspect_ratio` | 1 | 5,372 | 0.0% |
| `min_peak_hv` | 244 | 5,128 | 0.1% |
| `copol_dominance` | 679 | 4,449 | 0.3% |
| `clutter_contrast` | 44 | 4,405 | 0.0% |

This ledger is published rather than summarised because it shows something a
single aggregate figure would hide: **one stage does most of the work.**
CFAR at this Pfa produces predominantly isolated single-pixel speckle hits,
while genuine targets form multi-pixel clusters, so the minimum-size gate
carries the bulk of the suppression.

The recall cost of that threshold is **not measured**. Raising it removes
false alarms and small icebergs together, and without ground truth the
trade-off cannot be located (LIMITATIONS §9).

---

## Operating points

Detection density against the CFAR design false-alarm probability, before
and after the suppression chain. Published so the chosen operating point
can be read off a curve rather than taken on trust.

| Pfa | raw per 1000 km² | after suppression | scenes |
|---|---:|---:|---:|
| 1e-07 | 18.98 | 0.28 | 5 |
| 1e-06 | 34.91 | 0.47 | 5 |
| 1e-05 | 70.56 | 0.93 | 5 |
| 1e-04 | 163.40 | 2.10 | 5 |

![Operating points](benchmarks/operating_points.png)

---

## Reproducing

```bash
make fetch-shorelines   # GSHHG coastlines for land masking
make scene-index        # index the AI4Arctic archive by extent
make benchmark SWEEP=1  # run and write artefacts
python scripts/make_benchmark_doc.py
```

Scene selection requires the scene **centre** inside the AOI
(64.5°W–44.0°W, 42.5°N–60.5°N), not merely an overlap: an EW swath is about
400 km across and can clip the corner of the box while lying almost entirely
outside the region.

## Per-scene results

| scene | ice regime | wind | water km² | raw | kept | per 1000 km² |
|---|---|---|---:|---:|---:|---:|
| `20190406T102029_cis` | unknown | moderate | 41,860 | 1,136 | 62 | 1.48 |
| `20200217T102731_cis` | unknown | high | 86,029 | 2,894 | 121 | 1.41 |
| `20200319T101935_cis` | unknown | high | 151,448 | 4,810 | 193 | 1.27 |
| `20180331T212355_cis` | ice_affected | high | 168,099 | 22,301 | 28 | 0.17 |
| `20180410T214024_cis` | ice_affected | moderate | 54,724 | 4,291 | 62 | 1.13 |
| `20180428T093937_cis` | open_water | low | 156,384 | 4,083 | 125 | 0.80 |
| `20180619T104303_cis` | ice_affected | low | 61,666 | 4,115 | 104 | 1.69 |
| `20180621T214028_cis` | ice_affected | low | 66,042 | 7,741 | 153 | 2.32 |
| `20190206T212401_cis` | ice_affected | high | 154,088 | 7,426 | 18 | 0.12 |
| `20190216T214030_cis` | ice_affected | moderate | 56,742 | 4,194 | 121 | 2.13 |
| `20190306T102725_cis` | ice_affected | high | 143,793 | 6,513 | 206 | 1.43 |
| `20190308T101217_cis` | ice_affected | high | 48,062 | 1,566 | 44 | 0.92 |
| `20190326T212401_cis` | ice_affected | moderate | 152,446 | 8,059 | 106 | 0.69 |
| `20190401T101117_cis` | ice_affected | moderate | 147,172 | 6,111 | 358 | 2.43 |
| `20190405T214031_cis` | ice_affected | high | 48,670 | 3,880 | 90 | 1.85 |
| `20190411T102726_cis` | ice_affected | high | 145,153 | 4,646 | 128 | 0.88 |
| `20190507T101118_cis` | open_water | low | 157,526 | 3,010 | 48 | 0.30 |
| `20190507T101218_cis` | ice_affected | low | 59,743 | 2,118 | 131 | 2.19 |
| `20190524T101931_cis` | ice_affected | low | 142,369 | 3,144 | 30 | 0.21 |
| `20190612T101120_cis` | open_water | moderate | 146,424 | 3,244 | 30 | 0.20 |
| `20190612T101220_cis` | ice_affected | moderate | 52,206 | 1,827 | 129 | 2.47 |
| `20200201T212408_cis` | ice_affected | low | 155,095 | 20,012 | 333 | 2.15 |
| `20200203T104354_cis` | ice_affected | moderate | 114,597 | 1,404 | 61 | 0.53 |
| `20200224T102034_cis` | ice_affected | low | 37,633 | 718 | 47 | 1.25 |
| `20200306T214036_cis` | ice_affected | high | 50,533 | 3,174 | 52 | 1.03 |
| `20200319T102035_cis` | ice_affected | high | 37,625 | 1,425 | 55 | 1.46 |
| `20200413T212408_cis` | open_water | moderate | 167,985 | 10,118 | 7 | 0.04 |
| `20200415T104354_cis` | ice_affected | moderate | 57,665 | 2,464 | 150 | 2.60 |
| `20200423T214037_cis` | ice_affected | high | 109,822 | 20,703 | 511 | 4.65 |
| `20200424T101936_cis` | ice_affected | high | 148,250 | 4,670 | 140 | 0.94 |
| `20200424T102036_cis` | ice_affected | moderate | 37,626 | 1,498 | 90 | 2.39 |
| `20200506T101936_cis` | ice_affected | low | 123,674 | 2,953 | 45 | 0.36 |
| `20200506T102036_cis` | ice_affected | moderate | 37,617 | 1,440 | 93 | 2.47 |
| `20200517T214039_cis` | ice_affected | low | 59,789 | 493 | 0 | 0.00 |
| `20200523T102734_cis` | ice_affected | high | 133,106 | 4,086 | 270 | 2.03 |
| `20200610T214040_cis` | ice_affected | low | 76,604 | 8,016 | 144 | 1.88 |
| `20200624T212516_cis` | open_water | low | 72,778 | 1,190 | 6 | 0.08 |
| `20210407T101941_cis` | ice_affected | low | 143,132 | 3,480 | 112 | 0.78 |
| `20210530T102740_cis` | open_water | moderate | 84,388 | 1,283 | 2 | 0.02 |
