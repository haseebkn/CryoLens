# Data access and provenance

Use public data under its own terms; this repository's MIT license covers
software only. Do not redistribute licensed raw satellite imagery or private
AIS records in portfolio screenshots or Git without appropriate permission.

| Source | Purpose | Access and current use |
|---|---|---|
| [AI4Arctic ready-to-train](https://data.dtu.dk/articles/dataset/Ready-To-Train_AI4Arctic_Sea_Ice_Challenge_Dataset/21316608) | Real SAR and sea-ice context | Public DTU download; local archive used for research evaluation |
| [Publisher toolkit](https://github.com/astokholm/AI4ArcticSeaIceChallenge) | Normalization/data format contract | Public source; normalization provenance must accompany restored physical values |
| [GSHHG](https://www.soest.hawaii.edu/pwessel/gshhg/) | Shoreline and coastal exclusion for COG processing | Public 2.3.7 shapefiles; download with `make fetch-shorelines` |
| [Copernicus Data Space](https://dataspace.copernicus.eu/) | Fresh Sentinel-1 EW HH/HV SAFE imagery | Catalogue metadata is public; downloads require configured CDSE credentials |
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

The audit found local database credentials configured; CDSE, Earthdata and
Kaggle credentials were absent. The public archive supports research work
without adding those credentials. For fresh downloads, populate one supported
provider's values locally in `.env`; do not send passwords in chat. Credentials
alone do not resolve missing ground truth or make a detector operational.
