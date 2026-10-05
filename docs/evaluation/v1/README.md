# NL evaluation freeze v1

Frozen research scope, 5 October 2026. No final test evaluation has run.

| Partition | Unique input records |
|---|---:|
| Train | 39 |
| Validation | 10 |
| Test | 2 |
| Total | 51 |

The inputs form **33 acquisition groups**. Byte-identical copies are recorded
as source aliases, never extra scenes. All records carry actual source SHA256,
acquisition time, footprint provenance, exposure status and group assignment.

Reserved test inputs:

- `20190119T101218_cis_prep.nc`: newly acquired publisher-labelled winter
  scene, stored outside routine archive discovery. SAR/chart values remain
  uninspected. Valid chart coverage must be checked at locked evaluation time.
- `S1C_EW_GRDM_1SDH_20260912T090816_20260912T090908_009412_012B82_FF9F_COG.SAFE`:
  authentic downloaded SAFE archive with metadata/integrity-only exposure;
  calibrated processing and independent target/identity annotations remain pending.

These are pilots, not a regional performance sample. January and September
results cannot establish February–July peak-season performance. Detection,
segmentation and identity have separate reference requirements and metrics.

See the [sealed manifest](manifest.json), [coverage GeoJSON](coverage.geojson),
[coverage map](coverage.png), and [complete protocol](../../EVALUATION_PROTOCOL.md).
The [MLflow receipt](tracking_receipt.json) records the successful local scope
run and its provenance artifacts; it contains no model-performance metrics.
Footprint area includes land and quality exclusions not yet applied; it is not
eligible surveyed water. Identical source aliases can be reconstructed as copies
of the same verified bytes without adding acquisition exposure.

The exposure ledger records the initial download inadvertently selected by a
legacy regression fixture and the rejected duplicate replacement. Neither is
claimed as an untouched test. Routine integration tests now select permitted
development inputs.
