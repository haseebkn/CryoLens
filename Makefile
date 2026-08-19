.PHONY: help dev test lint format db-up db-down db-logs db-migrate clean

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

clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
