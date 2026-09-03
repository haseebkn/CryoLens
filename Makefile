.PHONY: help dev dev-all test lint format db-up db-down db-logs db-migrate preprocess cfar api slice ingest-iip pipeline correlate drift train-yolo benchmark fetch-shorelines scene-index clean

PYTHON ?= python
UV ?= uv

help:
	@echo "CryoLens Development Commands:"
	@echo "  make dev         - Install package in editable mode with development dependencies"
	@echo "  make dev-all     - Install package with development and ML dependencies"
	@echo "  make test        - Run test suite with pytest"
	@echo "  make lint        - Check code style and type annotations (ruff + mypy)"
	@echo "  make format      - Auto-format code with ruff"
	@echo "  make db-up       - Start PostGIS database container in background"
	@echo "  make db-down     - Stop PostGIS database container"
	@echo "  make db-logs     - Follow PostGIS database logs"
	@echo "  make preprocess  - Run SAR radiometric preprocessing on a scene (make preprocess SCENE=<id>)"
	@echo ""
	@echo "Detection and evaluation:"
	@echo "  make fetch-shorelines - Download GSHHG coastlines for land masking (150 MB, no credentials)"
	@echo "  make scene-index      - Index the AI4Arctic archive by geographic extent"
	@echo "  make benchmark        - Measure detection density per 1000 km2 over the NL AOI"
	@echo "                          Options: LIMIT=n PFA=1e-5 DETECTOR=gamma|ca SWEEP=1"
	@echo "  make cfar             - Run CFAR on a single preprocessed scene"
	@echo ""
	@echo "  make clean       - Remove cached bytecode, test artifacts, and build directories"

dev:
	@mkdir -p data/raw data/interim data/processed data/cache configs/snap
	$(UV) pip install -e ".[dev]"

dev-all:
	@mkdir -p data/raw data/interim data/processed data/cache configs/snap
	$(UV) pip install -e ".[dev,ml]"

test:
	pytest

lint:
	ruff check .
	mypy src tests

format:
	ruff format .
	ruff check --fix .

db-up:
	docker compose up -d postgis

db-down:
	docker compose down

db-logs:
	docker compose logs -f postgis

preprocess:
	$(UV) run python -m cryolens.preprocess $(if $(SCENE),--scene $(SCENE),) $(if $(ENGINE),--engine $(ENGINE),)

db-migrate:
	$(UV) run alembic upgrade head

cfar:
	$(UV) run python -m cryolens.detect $(if $(SCENE),--scene $(SCENE),) $(if $(PFA),--pfa $(PFA),) $(if $(DIST),--distribution $(DIST),)

api:
	$(UV) run uvicorn cryolens.api:app --host 0.0.0.0 --port 8000 --reload

slice: db-migrate
	$(UV) run python -m cryolens.detect $(if $(SCENE),--scene $(SCENE),)

ingest-iip:
	$(UV) run python -c "from cryolens.ingest.iip import IIPClient; from cryolens.db.session import get_db_session_factory; session = get_db_session_factory()(); IIPClient().ingest_csv(session, '$(CSV)')"

pipeline:
	$(UV) run python -c "from cryolens.pipeline import PipelineRunner; from datetime import datetime, UTC, timedelta; PipelineRunner().run_batch(start_date=datetime.now(UTC) - timedelta(days=7), end_date=datetime.now(UTC))"

correlate:
	$(UV) run python -c "from cryolens.eval.correlate import SpatiotemporalMatcher; from cryolens.db.session import get_db_session_factory; session = get_db_session_factory()(); SpatiotemporalMatcher().correlate_scene(session, '$(SCENE)')"

drift:
	$(UV) run python -m cryolens.drift $(if $(SCENE),--scene $(SCENE),) $(if $(HOURS),--hours $(HOURS),)

train-yolo:
	@echo "No trained YOLO model exists. See docs/LIMITATIONS.md section 7."
	@echo "Chip extraction needs a band_loader callable plus analyst-validated"
	@echo "detections in the database; call cryolens.detect.dataset.DatasetBuilder"
	@echo "directly once xView3-SAR and Statoil/C-CORE data are available."
	@exit 1

benchmark:
	$(UV) run python -m cryolens.eval $(if $(LIMIT),--limit $(LIMIT),) $(if $(PFA),--pfa $(PFA),) $(if $(DETECTOR),--detector $(DETECTOR),) $(if $(SWEEP),--sweep,)

# GSHHG full-resolution shorelines. Public download, no credentials required.
fetch-shorelines:
	@mkdir -p data/cache/gshhg
	@test -f data/cache/gshhg/gshhg-shp-2.3.7.zip || curl -sSL --retry 5 -o data/cache/gshhg/gshhg-shp-2.3.7.zip https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-shp-2.3.7.zip
	cd data/cache/gshhg && $(PYTHON) -c "import zipfile; z = zipfile.ZipFile('gshhg-shp-2.3.7.zip'); keep = ('GSHHS_shp/f/GSHHS_f_L1.', 'GSHHS_shp/f/GSHHS_f_L2.', 'GSHHS_shp/h/GSHHS_h_L1.'); z.extractall('.', members=[n for n in z.namelist() if n.startswith(keep)])"
	@echo "GSHHG shorelines ready in data/cache/gshhg/GSHHS_shp/"

scene-index:
	$(UV) run python -c "import logging; logging.basicConfig(level=logging.INFO); from cryolens.data.ai4arctic import build_scene_index; build_scene_index('data/raw/ai4arctic', 'data/processed/ai4arctic_scene_index.json')"

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
