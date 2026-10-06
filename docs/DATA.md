# Data access and provenance

The [reference pilot](reference-set/v1/README.md) uses verified real AI4Arctic
source bytes and existing hash-checked optical pairs. January held-out SAR is
prepared for human reference annotation only; sea-ice chart arrays and model
evaluation remain sealed. Native grids, imagery and human-response ledgers stay
outside Git; source hashes, sampling design and preparation receipts are versioned.

Use public data under its own terms; this repository's MIT license covers
software only. Do not redistribute licensed raw satellite imagery or private
AIS records in portfolio screenshots or Git without appropriate permission.

| Source | Purpose | Access and current use |
|---|---|---|
| [AI4Arctic ready-to-train](https://data.dtu.dk/articles/dataset/Ready-To-Train_AI4Arctic_Sea_Ice_Challenge_Dataset/21316608) | Real SAR and sea-ice context | Public DTU download; local archive used for research evaluation |
| [Publisher toolkit](https://github.com/astokholm/AI4ArcticSeaIceChallenge) | Normalization/data format contract | Public source; normalization provenance must accompany restored physical values |
| [GSHHG](https://www.soest.hawaii.edu/pwessel/gshhg/) | Shoreline and coastal exclusion for COG processing | Public 2.3.7 shapefiles; download with `make fetch-shorelines` |
| [Copernicus Data Space](https://dataspace.copernicus.eu/) | Fresh Sentinel-1 EW/IW HH/HV SAFE imagery | Catalogue metadata is public; downloads require configured CDSE credentials. Authentic products are available; processing acceptance remains blocked ([report](processing-validation/v1/README.md)) |
| [Planetary Computer Sentinel-2 L2A](https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a) | Paired optical review | Public STAC and real COG range reads; no new account credentials. B02/B03/B04/B08 at 10 m and SCL at 20 m are preserved as native crops with unsigned source URLs, timestamps and crop hashes ([report](optical-review/v1/README.md)) |
| [CDSE Sentinel-2 L2A](https://documentation.dataspace.copernicus.eu/APIs/OData.html) | Alternative optical downloads and direct product verification | Public OData catalogue; original SAFE ZIP downloads use existing CDSE credentials. Archive and manifest checks, declared BOA encoding and native crops are recorded. Exact-product versus same-acquisition reprocessing is explicit ([report](optical-review/cdse-v1/README.md)) |
| [NASA Earthdata / ASF](https://search.asf.alaska.edu/) | Alternative Sentinel-1 access | Downloads require Earthdata credentials |
| [NSIDC G00807](https://nsidc.org/data/g00807) | Historical IIP sightings as context | Follow source access terms; never substitute for matched iceberg labels |
| Timestamped regional AIS | Potential vessel deconfliction | Not connected; a suitable licensed or authorized feed is still needed |
| Coincident verified iceberg observations | Precision, recall and error budgets | Not present; requires provenance, location/time uncertainty and independent review |

## Local organization

Store raw NetCDFs under `data/raw/ai4arctic/` (subdirectories are supported),
shorelines under `data/cache/gshhg/`, and generated outputs under
`data/processed/`. Those directories are excluded from Git apart from
`.gitkeep`. Published small evidence artifacts belong in `docs/benchmarks/`.

Record the original source/product ID, source URL or DOI, acquisition time,
file checksum, preprocessing/normalization version, pixel spacing, masks,
Pfa and suppression settings with a run. Do not mix old benchmark output
with corrected output merely because filenames match. The historical demo
scene named `DEMO_SCENE` is not evidence and should not be presented.

## Credentials

CDSE credentials are configured locally and have been used for authentic
Sentinel-1 and Sentinel-2 downloads. Sentinel-2 paired review defaults to public
Planetary Computer access with temporary signatures kept in memory; selecting
`--provider cdse` uses `CDSE_USERNAME` and `CDSE_PASSWORD` from local `.env`.
No second satellite account is required. Analyst evidence writes need
`CRYOLENS_ANALYST_ID` and `CRYOLENS_ANALYST_API_KEY` configured locally; otherwise
the dashboard is read-only. Do not send passwords or keys in chat. Credentials
do not resolve missing ground truth or make a detector operational.
