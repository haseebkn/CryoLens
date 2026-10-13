# Complete corrected EW/IW output validation

This continuation closes the finite preprocessing milestone using the already downloaded northern Labrador EW and Grand Banks IW development observations. It validates complete corrected outputs; regional operational release remains a separate requirement.

## Verified outcome

**Roadmap step 2 passes for representative complete corrected EW/IW processing under the frozen limits.** [The final report](report.json) keeps regional deployment and scientific release closed. It evaluates the final BigTIFF outputs, including every base/overview pixel and all eighteen actual corrected-power SNAP comparisons.

| Mode | Complete output width × height | Source HV non-positive | Worst-window corrected reprojection p95 | Matched windows |
|---|---:|---:|---:|---:|
| EW | 14317 × 14252 | 2.692% | 0.011488 dB | 9/9 |
| IW | 7870 × 7005 | 2.337% | 0.056643 dB | 9/9 |

The complete output base rasters contain 92,854,683 valid EW pixels and 25,255,613 valid IW pixels. The surrounding footprint bounding rectangles include nodata; these counts are not NL eligible detection area or false-alarm denominators. Every replacement base pixel is identical to its original corrected counterpart. [The local output inventory](local-output-inventory.json) records paths, exact hashes and sizes; multi-gigabyte rasters remain local.

The eighteen reference receipts and logs represent eighteen distinct new SNAP executions. Rechecking them for replacement packaging does not add executions. Source-processing, reference-generation, packaging and final-comparison code snapshots are preserved separately. The scientific runs recorded the actual dirty checkout at parent commit `68b0a72`; later typing cleanup and review commits do not replace those generating bytes.

## Processing contract

The maintained Python command visits the entire source measurement grid in bounded stripes, applies the measured calibration and thermal-noise annotations, and writes the whole footprint in bounded destination blocks. Signed non-positive observations are counted before their invalid logarithmic support is masked. There is no positive floor or noise rescaling. Bilinear sampling operates in linear power, with a one-pixel source halo and common HH/HV/incidence validity; the ratio is computed from the final HH/HV decibels.

All four bands use EPSG:3978 at 40 m spacing. The extent covers all projected annotation knots with an explicit two-pixel margin. A hash-bound independent SNAP grid can set the grid phase, avoiding an additional interpolation during matched comparison. This changes neither measurements nor pixel spacing. It is recorded in the preprocessing configuration.

The output is a complete source-footprint research derivative. Target analysis and serving remain restricted to the shared NL marine AOI; the full raster is not provincial surveyed coverage. Marine mean-sea-surface geometry does not establish terrain or independent absolute ground accuracy.

## Unchanged checks

The source policy remains `safe-quality-v1`: at most 5% non-positive finite power, at least 10% finite observations, incidence 15Ã¢â‚¬â€œ50 degrees, and HH/HV annotation geolocation agreement within 20 m. These are project research thresholds.

The nine fixed 512Ãƒâ€”512 windows per product keep the existing matched-pixel limits: calibration and signed-noise p95 absolute error at most 0.02 dB, sign mismatch at most 0.0001, annotation differences at most 20 m, incidence differences at most 0.05 degrees, relative registration at most 40 m, and corrected reprojection p95 absolute error at most 1 dB with at least 4,096 matched pixels.

`validation-corrected-output-v1.xml` geocodes the **noise-corrected** calibration branch with actual SNAP 14. It preserves unclipped signed noise in the sensor-coordinate comparison. This is separate from the older raw-power geometric diagnostics.

Every full-resolution output pixel and every stored overview pixel is inspected for common nodata, finite values, incidence, ratio identity, CRS/grid and COG integrity. Separate source-scan, output-quality and processing receipts preserve their exact hashes. Occupied product output locations are refused. Source inputs are hashed before and after the complete execution; actual executing code and non-secret configuration are archived independently of later review fixes.

## Reproduce

Use `configs/sentinel1-complete-output-v1.json` and the existing authenticated products. Adjust paths only in a new versioned configuration and choose a new output directory. The preserved scratch views must match the original reference inventories and original annotation hashes. Frozen test acquisitions are refused by the scientific driver.

```powershell
$env:PYTHONPATH = "$PWD/src;$PWD"
python validation/validate_complete_products.py --config configs/sentinel1-complete-output-v1.json --output-dir E:/new-complete-validation --stage references
python validation/validate_complete_products.py --config configs/sentinel1-complete-output-v1.json --output-dir E:/new-complete-validation --stage process
python validation/validate_complete_products.py --config configs/sentinel1-complete-output-v1.json --output-dir E:/new-complete-validation --stage compare
```

The process stage invokes the same `python -m cryolens.preprocess` entry point used for ordinary preprocessing. The final comparison inspects the actual exported complete rasters against the verified SNAP reference files; structural validation alone does not establish acceptance.

## Release boundary

A successful report may mark representative EW/IW preprocessing complete. It does not enable fresh target detection, provide aligned sea-ice context, qualify every season or location, prove iceberg identities, establish precision/recall, apply precise orbit correction or issue navigation products. The fresh-detection tag and regional/scientific-release flags remain blocked.

Historical processing failures, scientific receipts, acquisition selection and human review ledgers are preserved unchanged. More acquisition hunting or a denoiser is not automatically required to close this bounded milestone.


## Large-raster packaging investigation

The first complete EW conversion logged classic-TIFF size errors in rio-cogeo's temporary overview dataset. The full-resolution corrected pixels passed all nine reference comparisons, and every stored overview subsequently passed bounded semantic checks. The first outputs and their receipts remain unchanged.

Future ordinary exports use GDAL's COG driver with explicit `BIGTIFF=YES`, lossless DEFLATE compression and freshly generated nearest overviews. The implementation also captures source provenance when invoked from an installed package without a Git checkout.

This investigation produces replacement packaging in new locations and verifies every full-resolution pixel, grid, band description, nodata value and scientific provenance against the original corrected export. A separate packaging receipt binds both file hashes and the actual packaging code. Replacement packaging is not a new calibration or an additional SNAP execution. The final comparison consumer independently repeats the whole-raster identity check and all eighteen matched corrected-output comparisons on the selected replacement files.

For this retained first-run history, insert the optional recovery stage before the final comparison:

```powershell
python validation/validate_complete_products.py --config configs/sentinel1-complete-output-v1.json --output-dir E:/existing-complete-validation --stage package --packaged-products E:/new-packaged-products
python validation/validate_complete_products.py --config configs/sentinel1-complete-output-v1.json --output-dir E:/existing-complete-validation --stage compare --packaged-products E:/new-packaged-products --report-dir E:/new-final-comparison
```

The new ordinary producer does not require this recovery stage. New processing runs produce BigTIFF directly. The documented packaging receipt chain keeps the original generating record distinct from its replacement container.
