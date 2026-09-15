# CryoLens

**Auditable satellite radar candidate screening for the Newfoundland and Labrador shelf.**

CryoLens is a research portfolio project for maritime remote sensing. It uses
Sentinel-1 HH/HV imagery to find bright radar targets, suppress common clutter,
and show evidence for analyst review. A detected target is **unclassified and
unverified** until reviewed. A detector score is not the probability of an iceberg.

The September 2026 audit found errors in preprocessing, geographic coverage,
classification claims and evaluation. Earlier headline results are withdrawn;
see [benchmark status](docs/BENCHMARK.md) and the [audit](docs/AUDIT.md).
Precision, recall and a real-world false-positive rate have **not** been established.

Across **25 Sentinel-1 acquisitions** (28 eligible, 3 fully masked by the
conservative open-water filter), the screening path retained **311 unverified
candidates** over **1,670,147 km²** of cumulative eligible coverage, or 0.186
per 1,000 km². This is a reproducible screening result, not a validated regional
false-alarm budget.

It supersedes an earlier three-acquisition smoke run, and is **worse**, not
better: retained density rose from 0.108 to 0.186 per 1,000 km² and the
suppression factor fell from 38.9x to 19.4x once coverage grew five-fold. Small
samples flattered the result.

## Relevance to C-CORE

The design is informed by C-CORE's public work on SAR target detection,
ship/iceberg discrimination and human quality control. This is an independent
project, with no C-CORE affiliation, endorsement or certification. The
[MDA alignment](docs/MDA_ALIGNMENT.md) maps public sources to implemented
behavior and identifies the missing operational capabilities.

## What you can inspect

- CA-CFAR and Gamma-CFAR statistics in linear power, with explicit nodata handling.
- A shared NL study polygon used for pixel-level analysis and API filtering.
- Land/coast, border, seam and optional sea-ice exclusion; an auditable suppression ledger.
- Unclassified candidate geometry and radiometry, source timestamps and review provenance.
- A FastAPI/Leaflet analyst dashboard with bounded queries and protected review writes.
- Tests, type checks, dependency locking, database migrations and real PostGIS checks in CI.

**Gamma-CFAR is not a K-distribution detector.** The historical `k_distribution`
configuration value is retained only as a compatibility alias. The default is `gamma`.

## Quickstart

Python 3.11, [uv](https://docs.astral.sh/uv/) and Docker are needed for the full
local app. Run from the repository root. These commands work in PowerShell
and POSIX shells; use your shell's copy command for `.env.example`.

```text
uv sync --frozen --extra dev
```

Copy `.env.example` to `.env`, set a local `POSTGRES_PASSWORD`, then:

```text
docker compose up -d postgis
uv run --frozen alembic upgrade head
uv run --frozen uvicorn cryolens.api:app --host 127.0.0.1 --port 8000
```

Open [the dashboard](http://localhost:8000) or [the API docs](http://localhost:8000/docs).
An empty database is shown as empty; the app does not manufacture detections.
For an interview walkthrough, use [PORTFOLIO.md](docs/PORTFOLIO.md).

With the public archive downloaded, import the first labeled example:

```text
uv run --frozen python scripts/import_ai4arctic_scene.py --scene data/raw/ai4arctic/train/20180331T212355_cis_prep.nc
```

This imports a historical 2018-03-31 observation, not a live feed. Select
**Unverified SAR candidates** in the dashboard to inspect its six candidates;
the default confirmed-only view correctly shows none until an analyst reviews
the evidence. Re-importing preserves existing records and reviews.

Review writes are disabled until both `CRYOLENS_ANALYST_API_KEY` and
`CRYOLENS_ANALYST_ID` are configured locally. Never put credentials in Git,
screenshots or presentation materials. Bind locally for a demo; production
hosting and identity management have not been assessed.

## Real data and evaluation

The local evaluation uses the public
[AI4Arctic ready-to-train dataset](https://data.dtu.dk/articles/dataset/Ready-To-Train_AI4Arctic_Sea_Ice_Challenge_Dataset/21316608),
which contains Sentinel-1 scenes and **sea-ice charts, not iceberg truth**.
The publisher's normalization must be inverted using its documented constants;
scene extrema alone cannot establish physical units. All results depend on
that data contract and the masking assumptions documented in the benchmark.

```text
uv run --frozen python -m cryolens.eval --help
uv run --frozen python -m cryolens.detect --help
uv run --frozen python -m cryolens.preprocess --help
```

Raw imagery, credentials, trained weights and generated databases are excluded
from Git. GSHHG shorelines are public and are required by the raw COG detection
path (`make fetch-shorelines` on systems with Make). See
[DATA.md](docs/DATA.md) for access and provenance.

The raw SAFE/COG path lacks aligned sea-ice context and requires explicit
unknown-ice research opt-in (`--allow-unknown-ice` for the detection CLI,
`PipelineRunner(allow_unknown_ice=True)` in Python). The charted public-scene
import above uses conservative open-water screening by default.

## Geographic scope

The hand-defined study polygon covers the Labrador coastal corridor, Northeast
Newfoundland Shelf and Grand Banks. It is stored in `configs/aoi.geojson` and
bounded by 60.5°W–44°W, 42.5°N–60.5°N. It is **not a provincial boundary or EEZ**.
Land and coastal exclusion are separate from this marine research area.
Intersecting scenes may be loaded, but pixels and candidate centres outside
the study area are excluded. Coverage is reported as analyzed area; masked
water is not evidence that no icebergs are present.

## Validation

```text
uv run --frozen pytest
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src tests
uv run --frozen python scripts/check_postgis.py
uv build
```

The PostGIS check requires the migrated local database and rolls back its
fixtures. Unit tests also use SQLite substitutes; those alone do not verify
PostGIS. Real-data integration tests skip explicitly when their source files
are absent. CI uses locked dependencies and a real PostGIS service.

## Capability boundaries

| Capability | Honest status |
|---|---|
| Statistical radar candidate screening | Implemented; thresholds require regional validation |
| Analyst review and geographic API | Implemented; local API-key protection |
| SAFE calibration and GCP geolocation | Research implementation; not operationally validated |
| Precise-orbit correction in Python pipeline | Not applied; never claimed in provenance |
| Trained ship/iceberg classifier | Not implemented |
| Live AIS deconfliction | Not connected; no match does not establish iceberg identity |
| Drift prediction / grounding | Disabled; verified forcing and skill assessment missing |
| Navigational warnings / hazard advisories | Not produced |

Reducing candidate counts can also remove real icebergs. The project does not
claim that its thresholds minimize false positives or preserve recall.
[Limitations](docs/LIMITATIONS.md) describe the evidence needed to make those claims.

## Structure

```text
src/cryolens/
  config/       validated settings; credentials remain local
  ingest/       satellite catalogues, download/cache, IIP context
  preprocess/   SAFE calibration, geolocation, coast masks, COG stack
  data/         AI4Arctic data contract and scene index
  detect/       CFAR, suppression, scene runner, training export
  geo/          shared NL area and target geometry
  eval/         candidate-density reports and contextual matching
  api/ db/ web/ analyst interface, persistence and provenance
  drift/        explicit unavailable interfaces
```

Software: [MIT](LICENSE). Source datasets retain their own terms.
No output is suitable for navigation or an ice-hazard advisory.
