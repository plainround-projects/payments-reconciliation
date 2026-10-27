# make up | db | run | reconcile-sample | queries | serve | test | test-integration | test-e2e | lint | audit | down   (make help lists all)
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
POSTGRES_USER ?= reconciliation
POSTGRES_DB ?= reconciliation
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
CLI := $(VENV)/bin/payments-reconciliation

-include .env
export

.PHONY: help install lock env up db reset-db migrate queries run reconcile-sample serve health lint format test \
        test-integration test-e2e audit check docker-build down clean

help: ## list the targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@

install: $(STAMP) ## create .venv from the lock file

lock: ## regenerate the hash-pinned lock files from pyproject.toml (needs uv)
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
	uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 -o requirements-dev.lock

env:
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

up: env $(STAMP) ## start PostgreSQL and load sql/001_schema.sql and sql/002_seed.sql
	docker compose up -d --wait postgres
	$(MAKE) --no-print-directory db

reset-db:
	$(PSQL) < sql/reset.sql

db: reset-db ## recreate the schema from sql/001_schema.sql and load sql/002_seed.sql (drops existing data)
	$(PSQL) < sql/001_schema.sql
	$(PSQL) < sql/002_seed.sql

# The lesson's stepwise path: apply sql/migrations/*.sql, in order, to a scratch database (reconciliation_migrated).
migrate: ## apply sql/migrations/*.sql to a scratch database and list its tables
	-docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "DROP DATABASE IF EXISTS reconciliation_migrated"
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "CREATE DATABASE reconciliation_migrated"
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql -U $(POSTGRES_USER) -d reconciliation_migrated -v ON_ERROR_STOP=1 -q < $$file; done
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d reconciliation_migrated -c "\dt"

queries: ## run sql/003_queries.sql
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 < sql/003_queries.sql

# The lesson's whole scenario: a clean day, the planted day (500 payments, six breaks), resolution twice, the report,
# the next day. Resets the database and the settlement folder first.
run: $(STAMP) db ## the lesson's scenario end to end, including the planted breaks (resets the database)
	rm -rf var/settlement
	$(CLI) demo
	$(MAKE) --no-print-directory queries

# Reconciles the seeded day (sql/002_seed.sql) against data/settlement_2026-10-19.csv: one fee difference.
reconcile-sample: $(STAMP) ## reconcile the seeded sample day against its sample settlement file
	mkdir -p var/settlement && cp data/settlement_2026-10-19.csv var/settlement/
	$(CLI) reconcile --date 2026-10-19
	$(CLI) resolve --date 2026-10-19
	$(CLI) report --date 2026-10-19

serve: $(STAMP) ## run the HTTP API in the foreground (Ctrl-C or SIGTERM stops it gracefully)
	$(VENV)/bin/payments-reconciliation-serve

health: $(STAMP) ## exit 0 when the running API reports healthy
	$(VENV)/bin/payments-reconciliation-health

lint: $(STAMP) ## ruff check, ruff format check, mypy, import-linter
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports

format: $(STAMP) ## apply ruff's formatting and safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

# Unit tests need no services. The domain and application must stay at 95% coverage or more.
test: $(STAMP) ## unit tests with coverage (no database needed)
	$(PY) -m pytest tests/unit --cov=payments_reconciliation --cov-report=term
	$(PY) -m coverage report --include='src/payments_reconciliation/domain/*,src/payments_reconciliation/application/*' --fail-under=95

test-integration: $(STAMP) ## adapters and the lesson's scenario against the running database (make up first)
	$(PY) -m pytest tests/integration -m integration

test-e2e: $(STAMP) ## the program and a real server process against the running database (make up first)
	$(PY) -m pytest tests/e2e -m e2e

audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock

check: lint test ## everything that needs no services

docker-build: ## build the container image of the API
	docker build -t payments-reconciliation:dev .

down: ## stop PostgreSQL and delete its data
	docker compose --profile app down -v

clean: down
	rm -rf $(VENV) logs var .pytest_cache .mypy_cache .ruff_cache .coverage
