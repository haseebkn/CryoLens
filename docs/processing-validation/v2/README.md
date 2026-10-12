# Marine Sentinel-1 processing correction, v2

**The fresh-observation deployment gate remains closed.** This investigation
corrects processing and reference-workflow errors while retaining every v1
quality and comparison threshold. It does not qualify noise-limited data by
clipping, filling, dropping valid negative pixels or reducing noise amplitudes.

## Corrections

1. **Height datum and geocoding:** annotation-GCP warping was an approximation
   to a different surface. The SNAP reference with these settings uses EGM96
   sea-surface height when the annotated scene-height value is zero. Marine
   processing now solves zero-Doppler time from the declared Earth-fixed orbit,
   inverts annotated ground/slant-range polynomials and uses that same explicit
   mean-sea-surface height. The auxiliary grid is pinned and packaged, with
   [its measured retrieval checksum](../../../configs/auxdata/egm96/README.md).
2. **Timing:** the declared Sentinel-1 IPF bulk correction is referenced to
   near range. The remaining range-dependent propagation delay is applied using
   the original full-product reference range. No image displacement is fitted.
3. **Resampling:** the new path samples bilinear point values in linear power,
   then converts to dB and derives the HH/HV ratio. GDAL's scale-dependent warp
   kernel was not equivalent to the reference's point interpolation, particularly
   when going from 10 m IW spacing to 40 m output spacing. Missing or invalid
   interpolation support remains nodata in all four bands.
4. **Reference cropping:** the old graph cropped before range-Doppler processing.
   SNAP's subset changes near-range metadata, including the range used by the
   residual timing correction. V2 geocodes the full acquisition geometry and
   crops the output afterward. Raw calibration and signed noise comparisons still
   use matched source windows. **The old cropped-reference geometry acceptance
   decisions are superseded; they cannot validate the current marine path.**
5. **Short noise blocks:** a real September 11 development acquisition has an
   EW3 azimuth block spanning lines 224–322 with no interior range-noise vector.
   The reader now selects the nearest qualified vector within the declared
   subswath extent, retaining its measured range knots. It does not synthesize
   noise when no qualified annotation is available. A fresh SNAP run on this
   block agrees to better than 0.00001 dB, with no residual-sign mismatch.
6. **Nodata borders:** noise coverage is required for every actual valid
   measurement. Unannotated padding is allowed only where the raster mask or
   zero-DN declaration already establishes nodata. A valid observed pixel without
   noise annotation still rejects the input. The 10% minimum measurement coverage
   and 5% maximum non-positive fraction are unchanged.

The operational preprocessing entry points use the new marine mapping and export
its annotation, geoid, timing and resampling provenance. The GCP helper remains
available for explicit experiments. Unsupported timing, nonzero reference terrain
heights, incomplete orbit/range metadata and failed convergence are handled
explicitly. Precise-orbit adjustment, terrain normalization, tidal correction and
independently surveyed absolute geolocation are not established.

## Scientific references

The formulas and conventions follow the publisher's
[Sentinel-1 geocoding guide](https://sentinels.copernicus.eu/documents/247904/1653442/Guide-to-Sentinel-1-Geocoding.pdf),
particularly the range-dependent bistatic timing discussion, and the actual
[SNAP 14.0.0 geometry implementation](https://github.com/senbox-org/microwave-toolbox/blob/14.0.0/sar-op-sar-processing/src/main/java/eu/esa/sar/sar/gpf/geometric/RangeDopplerGeocodingOp.java).
The Python inverse equations are implemented independently; they do not embed
SNAP source code or use reference-image offsets to tune coordinates.

Noise handling follows [ESA MPC-0392 v2.2](https://sentiwiki.copernicus.eu/__attachments/1673968/DI-MPC-TN-MPC-0392%20-%20Thermal%20Denoising%20of%20Products%20Generated%20by%20Sentinel-1%20IPF%202024%20-%202.2.pdf),
section 6.3.2.2. The document also explains that subtracting mean annotated noise
can produce negative individual observations. Keeping signed residuals makes
this measurable; agreement with SNAP alone does not establish usability for
the project's CFAR detector.

## Remaining gate

Fresh SNAP execution and matched-pixel checks completed on all **27** predeclared
windows. Calibration, signed noise, annotation/incidence, relative registration,
warped radiometry and diagnostic COG integrity pass their unchanged limits.

| Acquisition | Passed windows | Worst window p95 warped-power error | Limit |
|---|---:|---:|---:|
| Legacy EW | 9/9 | 0.00550 dB | 1 dB |
| Fresh EW | 9/9 | 0.00637 dB | 1 dB |
| IW | 9/9 | 0.03873 dB | 1 dB |

Measured whole-pixel texture displacement is zero in these windows. This does
not establish zero absolute ground error. The comparison preserves full-source
geometry but evaluates selected windows; it does not certify whole-footprint
gridding or surveyed absolute geolocation.

![Fresh source-window diagnostics and retained deployment rejection](spatial-diagnostics.png)

Fresh full-product measurement scans retain the following rejections:

| Product | HV non-positive fraction | Strict limit | Outcome |
|---|---:|---:|---|
| Legacy EW, 2018-04-28 | 45.73% | 5% | Reject |
| Fresh EW, 2026-09-16 | 26.09% | 5% | Reject |
| IW, 2026-09-27 | 54.3% | 5% | Reject |
| Additional EW, 2026-09-11 | 7.63% | 5% | Reject |

These fractions describe finite observations in full measurement grids, including
areas outside the NL study polygon. They are not provincial eligible-water
statistics or false-alarm rates. Some channels also have additional rejection
reasons; the complete reports preserve those.

The fourth scene now finishes its full scan after the noise-block/nodata fixes;
its earlier metadata rejection was a reader bug. Its whole-product geometry has
not been qualified by the short-block noise comparison.

The original NERSC experiment remains unqualified for deployment: preserving its
signed output left 59.6% non-positive HV observations on the tested S1B product.
No new C/D coefficient qualification is claimed here. There is no defensible
pixel-level reconstruction of unobserved weak clutter in this change.

To complete roadmap step 2, demonstrate representative usable EW and IW marine
observations passing these same frozen limits, or independently qualify a
different noise-processing method under those limits. Record unusable acquisitions
as rejections. Absolute field geolocation and broader seasonal/spatial coverage
remain separate limitations. More credentials are not the current blocker.

## Reproduce

Use the documented SNAP 14 image and the authentic development products. Neither
driver accesses held-out detector inputs or reviewer workspaces.

For an ordinary checkout with raw SAFE products at the configured locations,
prepare the lossless reference scratch view and run the standard validator:

```powershell
python -m cryolens.preprocess.validation --config configs/sentinel1-validation-v2.json --output-dir data/processed/safe-validation-v2 --scratch-dir data/interim/safe-validation-v2-input --run-reference
```

It exits with failure while any required product fails quality. This is the
expected result for these inputs. The investigation driver used the previously
verified scratch views in the evidence checkout and fresh v2 reference outputs:

```powershell
python validation/validate_marine_processing.py --evidence-root E:\CryoLens --reference-root data/processed/new-marine-validation/reference --output data/processed/new-marine-validation --run-reference
```

The [v2 graph](../../../configs/snap/validation-reference-v2.xml) preserves the
full range/timing geometry. All cache receipts include the geographic output
subset, graph, runtime, scratch input bytes and complete output inventories.
Changing the recipe invalidates old caches. Each output directory must be new.

The published [report](report.json) records fresh full scans and matched-window
checks. Raw rasters, runtime logs and diagnostic COGs remain in local data.
[Executed source](executed-code.zip) preserves the exact preprocessing modules,
driver and graph whose hashes are in the report. It was captured before a
type-only rename of the nearest-vector ordering variable; it does not pretend
that a future clean Git commit generated the measurements.

The [additional-scene report](short-block-report.json) records the tested short
noise block, original and adapted scratch provenance, signed comparisons and
full-product quality. Its independently generated reference remains bound by
[the short-block receipt](reference-receipts/short-block-receipt.json).

The [MLflow receipt](tracking_receipt.json) records real completed run
`9f59fb74ce6c42c1aed5c1fabefc9d56` in `cryolens-processing-v2`, logged from the
clean `4ee9a50` checkout. This is post-hoc evidence logging, with the frozen scope,
configuration, source snapshot, reference recipes, measured quality diagnostics
and report hash. It does not claim that `4ee9a50` generated the earlier execution
receipts, and logs no detector/model performance.

Local verification: 344 portable tests passed (14 archive-dependent tests skipped
in the isolated checkout), mypy passed 120 files, Ruff and formatting passed,
and the built wheel decoded its packaged pinned geoid outside the repository.
Analytic geometry, sampling/nodata and malformed-metadata tests exercise real
functions. Short-block and padding regressions failed before their fixes. The
27 real SNAP comparisons provide separate scientific processing evidence.
