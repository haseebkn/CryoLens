# Project audit — 2026-09-05

## Outcome and scope

CryoLens has been audited as an independent Newfoundland and Labrador SAR
research portfolio. The audit covered all tracked Python modules, API/database
paths, migrations, dashboard assets, configuration, packaging, CI and project
claims. It does not establish operational reliability, C-CORE certification,
precision or recall. The current limits are explicit rather than filled with
synthetic successful results.

## Material findings addressed

| Area | Finding | Change |
|---|---|---|
| Physical data | Observed scene extrema were used to infer standardized SAR units | Restore publisher mean/std constants with source checksum; test reader behavior |
| SAFE radiometry | Invented noise profiles could be subtracted after thermal correction | Require measured noise; apply real range/azimuth LUTs; preserve invalid pixels |
| Geolocation | Conflicting affine/GCP arguments and inconsistent pixel centres | Correct GCP reprojection and explicit CRS handling; retain research status |
| Failure behavior | Fake orbit files, SNAP success markers and synthetic CLI imagery | Missing prerequisites and unsupported chains fail explicitly |
| False positives | Invalid samples could corrupt CFAR statistics; Gamma fit incorrect | Finite-mask sanitization, corrected moments/quantiles, training-support coverage |
| Identity | Bright returns called icebergs; missing metrics invented | Unclassified candidates, nullable metrics and explicit score semantics |
| Geographic focus | Scene-centre rectangle allowed out-of-region analyzed pixels | Shared NL shelf polygon used by pixel processing and spatial API queries |
| Ice context | Unknown sea ice treated like usable water | Conservative mode excludes unknown/ice-affected water; raw path needs explicit research opt-in |
| Suppression | Hidden filtering and incomplete coverage accounting | Ordered removal ledger and eligible-area denominator |
| Learning data | Train/validation directories identical; undecided labels became clutter | Scene-disjoint split; final/latest verdicts and real pixel chips required |
| IIP association | Context could overstate identity; parsing/matching ambiguity | Regional/time validation and cautious association; no truth-label claim |
| Forecasting | Constant invented current, wind and bathymetry | Forecasts disabled, including public legacy trajectory serving |
| API review | Unauthenticated writes with a fabricated analyst identity | Configured API key, named identity, evidence notes and append-only reviews |
| API behavior | Unbounded/invalid spatial queries, unsafe tooltip HTML, leaked DB errors | Validated queries, pre-pagination NL filtering, safe rendering and sanitized health |
| Presentation | Unsupported operational language, fabricated defaults, broken basemap | Research dashboard, explicit unknowns, real public OSM map, source timestamps |
| Packaging | Runtime config unavailable outside checkout | Config/AOI bundled in wheel and tested from a separate working directory |
| Reproducibility | Unlocked CI install, mocked spatial behavior, absent license file | Frozen dependency install, real PostGIS check, distributable checks and MIT file |

## Evidence and interpretation

The evaluation has since been extended from three acquisitions to every
geographically eligible scene in the local public AI4Arctic archive, with the
same settings (Gamma-CFAR, Pfa 1e-6, open-water-only screening). Of 28 eligible
scenes, 25 were processed; 3 retained no eligible water after the AOI, quality
and ice masks and are recorded in the run manifest with their mask breakdowns.
The run retained 311 unverified candidates from 6,022 raw connected components
over 1,670,147 km² of accumulated eligible coverage. No scene was selected on
its target count, and no threshold was tuned against the outcome.

The expanded result is worse than the smoke run it replaces: retained density
rose from 0.108 to 0.186 per 1,000 km² and the suppression factor fell from
38.9x to 19.4x across roughly five times the coverage. That direction is the
point of expanding the sample. It remains a reproduction of a processing path,
not regional performance validation or an optimized operating point.

Two limits are now measurable rather than suspected. Four of the six suppression
stages removed nothing across all 25 scenes, and a fifth removed one candidate;
the minimum-component-size gate accounts for 94.8 percent of all removals, so
the remaining stages are presently unexercised on real data rather than
demonstrated. Candidate density by wind tercile is also not monotonic (0.199 low,
0.252 moderate, 0.109 high per 1,000 km²), which does not support a wind-driven
clutter narrative at this sample size.

An earlier three-scene challenge-test attempt had no usable charted open-water
coverage; all three were skipped and no density report was issued. Its
[manifest](benchmarks/withheld_scene_manifest.json) is preserved. The successful
[report](BENCHMARK.md) and [raw evidence](benchmarks/audited_results.json) record
source IDs, acquisition dates, checksums, settings and masks. The original
39-scene headline and operating-point plot are superseded, not comparable.

## Validation record

Validation includes behavioral regressions for the findings above, real
PostGIS migrations and spatial round-trips with rolled-back fixtures, package
build and out-of-checkout runtime checks, plus browser inspection of empty and
real-data states. Local validation on 2026-09-05 passed 221 tests, Ruff lint
and formatting (100 files), mypy (87 source files), the lockfile check, the
real PostGIS check, and the built-wheel runtime check outside the checkout.
The test run emitted 19 warnings, including synthetic-raster georeferencing,
Starlette/httpx deprecation and a netCDF4/NumPy binary-size warning; real-source
reads and checks completed successfully. Hosted CI is reported with the PR.
Synthetic test arrays exercise behavior; they are not presented as satellite
performance measurements. Real-source tests skip explicitly when data is absent.

## Remaining prerequisites

A credible operational claim requires independently reviewed acquisition-matched
iceberg and vessel truth, missed-target assessment, regional/seasonal held-out
evaluation, AIS and infrastructure deconfliction, geolocation error checks and
agreed false-alarm/recall criteria. Fresh satellite downloads need CDSE or Earthdata credentials configured
locally; CDSE credentials were subsequently configured and the SAFE reader
was cross-checked against NERSC processing of the same acquisition (see
LIMITATIONS.md section 6). No private credentials were added to
Git. No C-CORE internal procedures or proprietary interfaces were supplied.

See [MDA_ALIGNMENT.md](MDA_ALIGNMENT.md), [DATA.md](DATA.md),
[LIMITATIONS.md](LIMITATIONS.md) and [PORTFOLIO.md](PORTFOLIO.md).
