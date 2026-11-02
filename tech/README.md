# Technology notes

These notes teach every technology used in this repository, from zero, through the repository's own files. Each note says what the technology is, why it is here, where it appears, how it works underneath, walks through this repository's own lines, gives commands to try with the output to expect, lists common mistakes, and ends with five questions with answers. You need no earlier knowledge.

Run the commands from the repository root. Commands marked as needing the database expect `make up` first.

## If you have ten minutes

1. [python.md](python.md): the language, and how money (integers, immutable values), the matching rules and the break causes are written.
2. [postgresql.md](postgresql.md): how the database itself keeps the ledger balanced and a break unique per day, transaction, kind and cause.
3. [csv.md](csv.md): the provider's settlement report, read strictly line by line.

## Reading order

### 1. The language and how the project is packaged

| Note | Role here |
| --- | --- |
| [python.md](python.md) | The language of all code: dataclasses, enums, protocols, generics, context managers. |
| [pip-venv-and-lock-files.md](pip-venv-and-lock-files.md) | Installing exact, hash-checked dependencies into a private environment. |
| [uv.md](uv.md) | The tool that regenerates the lock files (`make lock` only). |
| [toml.md](toml.md) | The `pyproject.toml` format: project data and every tool's settings. |
| [environment-variables-and-dotenv.md](environment-variables-and-dotenv.md) | How configuration and the database password reach the program. |

### 2. The web layer

| Note | Role here |
| --- | --- |
| [http.md](http.md) | Requests, status codes and the endpoints of the API. |
| [json.md](json.md) | The body format of the API's responses, and why amounts are integers and strings. |
| [csv.md](csv.md) | The provider's settlement report: reading it strictly, writing it, one file per day. |
| [fastapi.md](fastapi.md) | The framework that turns functions into the payments API. |
| [pydantic.md](pydantic.md) | The validation library inside FastAPI (used indirectly here). |
| [uvicorn.md](uvicorn.md) | The server process, and the graceful shutdown. |
| [httpx.md](httpx.md) | The client side: the health check and the end-to-end tests. |
| [logging-and-logfmt.md](logging-and-logfmt.md) | Structured logs with card-number masking. |

### 3. The database

| Note | Role here |
| --- | --- |
| [postgresql.md](postgresql.md) | The server: transactions, constraints, deferred triggers, locks, UTC sessions. |
| [sql.md](sql.md) | The language of the `sql/` folder: schema, seed, queries, migrations, upserts. |
| [psql.md](psql.md) | The command-line client that loads the SQL files. |
| [psycopg.md](psycopg.md) | The Python driver, connection pool and pipeline mode. |

### 4. Running it

| Note | Role here |
| --- | --- |
| [make.md](make.md) | The `Makefile`: one short name per task. |
| [shell.md](shell.md) | The shell syntax inside Makefile recipes and health checks. |
| [docker.md](docker.md) | Containers and images in general. |
| [dockerfile.md](dockerfile.md) | The recipe that builds the API image. |
| [docker-compose.md](docker-compose.md) | The file that starts PostgreSQL (and optionally the API). |
| [yaml.md](yaml.md) | The format of the Compose file and the CI workflow. |

### 5. Proving it works

| Note | Role here |
| --- | --- |
| [pytest.md](pytest.md) | The test runner and the three test layers (pure unit tests, PostgreSQL, the whole scenario). |
| [coverage-and-pytest-cov.md](coverage-and-pytest-cov.md) | The 95 percent bar on domain and application. |
| [ruff.md](ruff.md) | Linting and formatting. |
| [mypy.md](mypy.md) | Static type checking. |
| [import-linter.md](import-linter.md) | Enforcing the layers: the domain must not know about the web or the database. |
| [pip-audit.md](pip-audit.md) | Checking locked dependencies for known vulnerabilities. |
| [github-actions.md](github-actions.md) | The CI workflow that repeats all of the above. |

### 6. Housekeeping

| Note | Role here |
| --- | --- |
| [git-and-ignore-files.md](git-and-ignore-files.md) | History, `.gitignore`, `.dockerignore`. |
| [editorconfig.md](editorconfig.md) | Editor settings (tabs for `Makefile`, spaces elsewhere). |
| [markdown.md](markdown.md) | The format of the README, architecture notes and decision records. |

### 7. Line-by-line walkthroughs of every file

These notes quote every source, SQL, test and configuration line of the repository and explain it in plain words. Read the general note first (for example `python.md`), then the walkthrough.

| Note | What it quotes |
| --- | --- |
| [python-domain.md](python-domain.md) | Every file of the `domain` package: money, ledger, payments, settlement rows and CSV, matching and classification. |
| [python-application.md](python-application.md) | The use cases (record a capture, reconcile a day, resolve breaks, daily report, worklist, health), the views and the ports. |
| [python-infrastructure.md](python-infrastructure.md) | Settings, logging, retry, settlement files, the stub provider, the day simulator and the PostgreSQL adapters. |
| [python-interfaces.md](python-interfaces.md) | The composition root, the HTTP app, the server launcher, the health command and the command-line program with the demo. |
| [sql-schema.md](sql-schema.md) | `sql/reset.sql` and `sql/001_schema.sql`. |
| [sql-seed-and-queries.md](sql-seed-and-queries.md) | `sql/002_seed.sql` and `sql/003_queries.sql`. |
| [sql-migrations.md](sql-migrations.md) | Every file in `sql/migrations/`. |
| [pytest-unit-tests.md](pytest-unit-tests.md) | The fixtures, fakes and every unit test. |
| [pytest-integration-and-e2e-tests.md](pytest-integration-and-e2e-tests.md) | The fixtures and every integration and end-to-end test. |

The listings of `Makefile`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml` and `pyproject.toml` sit inside [make.md](make.md), [dockerfile.md](dockerfile.md), [docker-compose.md](docker-compose.md), [github-actions.md](github-actions.md) and [toml.md](toml.md).

## A note on versions

The notes describe the versions this repository pins: Python 3.12 or newer (the image uses 3.12.8; development here used 3.13), PostgreSQL 18, FastAPI 0.162, Starlette 1.10, Pydantic 2.14, psycopg 3.3, psycopg_pool 3.3, httpx 0.28, uvicorn 0.54, pytest 9.1, mypy 2.4, Ruff 0.17, import-linter 2.15, pip-audit 2.10. The exact versions are in `requirements.lock` and `requirements-dev.lock`.
