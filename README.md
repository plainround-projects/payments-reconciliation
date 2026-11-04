# payments-reconciliation

A daily reconciliation job for a small payments service: it compares the ledger with the settlement report a payment provider sends, joins the two on the provider's own transaction id, classifies every difference by its cause, fixes the ones that can be fixed safely from the report's own data (once, never by calling the provider again), and sends only the ones that need a decision to a worklist, with a daily report that balances to the provider's settled total.

| | |
| --- | --- |
| **Lesson** | `/prep/payments/project-reconciliation`, "A reconciliation system for a small payments service" (`data-structure-explainer/src/content/tracks/payments/13-project-reconciliation.md` in the Plainround app) |
| **Planned hours** | 10 |
| **Level** | Intermediate |
| **Architecture** | [ARCHITECTURE.md](ARCHITECTURE.md), decisions: [docs/adr](docs/adr) |
| **Technology notes** | [tech/](tech/README.md): one beginner note per technology used here |

## Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/architecture-dark.svg">
  <img alt="The Matching job joins the Settlement report and Your ledger on the provider's transaction id and classifies each difference. Timing and fees are cleared by rules, Real breaks are fixed from the report, both writing balanced entries to the ledger, and what needs a person goes to the Worklist and the Daily report." src="docs/diagrams/architecture.svg">
</picture>

Every difference is classified by cause first, then either cleared by a rule, fixed from the report and never from the provider, or left on the worklist for a person.

## How one run flows

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/flow-dark.svg">
  <img alt="make run reconciles a clean day, then a planted day with six breaks, resolves it twice to show nothing changes, prints a daily report that balances with 4.99 open, and reconciles the next day." src="docs/diagrams/flow.svg">
</picture>

The amber rows are the six breaks; the green box is the carried timing break clearing itself on the next day, leaving only the amount mismatch on the worklist.

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Docker with Compose v2 | 24 or newer (tested with 24.0.6) | PostgreSQL, and the optional API image |
| Python | 3.12 or 3.13 (tested with 3.13; the CI uses 3.12) | the job, the program and the tests |
| GNU Make | 3.81 or newer | every command below |
| PostgreSQL image | `postgres:18` | pulled on first `make up` |
| Python packages | `fastapi`, `uvicorn`, `psycopg[binary,pool]` 3.2 or newer and `httpx` at run time; `pytest`, `pytest-cov`, `ruff`, `mypy`, `import-linter`, `pip-audit` for development. All pinned with hashes in `requirements.lock` and `requirements-dev.lock`; `make` installs them into `.venv/` | everything |

No cloud account and no provider account: the provider is a deterministic stub that settles in daily batches. The database password is read from the environment, never from code: `make up` copies `.env.example` to `.env` the first time (a throwaway value for a database that lives only on this machine while the container runs). Ports 54612 (PostgreSQL) and 8462 (the API when you run `make serve`) must be free; both are set in `.env`.

## Setup, run, test, stop

```bash
make up                  # PostgreSQL, then sql/001_schema.sql and sql/002_seed.sql
make run                 # the lesson's scenario: a clean day, the planted day with six breaks, resolving twice, the report, the next day
make reconcile-sample    # the seeded sample day (data/settlement_2026-10-19.csv): one fee difference, found, fixed, reported
make queries             # sql/003_queries.sql: trial balance, the zero-sum check, breaks by cause, the worklist, the runs
make serve               # the HTTP API in the foreground on http://127.0.0.1:8462 (Ctrl-C stops it gracefully)
make health              # exit 0 when the running API reports healthy
make check               # format check, lint, types, import rules, unit tests with coverage (no services needed)
make test-integration    # the schema's rules and the resolvers against PostgreSQL (needs make up; resets the database)
make test-e2e            # the whole scenario through the program and a real server process (needs make up; resets the database)
make audit               # known-vulnerability check of the locked dependencies
make migrate             # the stepwise migrations applied to a scratch database
make down                # stops the container and deletes its data
```

`make help` lists every target. `make run` starts with `make db`, which drops and recreates the schema, so each run is independent; `make test-integration` and `make test-e2e` do the same, so do not point them at a database you care about. Operational log lines (structured `logfmt`) go to standard error; results go to standard output.

The program's commands, one at a time (the lesson's `simulate_day.py`, `reconcile.py` and `resolve.py`):

```bash
set -a; . ./.env; set +a
.venv/bin/payments-reconciliation simulate-day --date 2026-10-20 --payments 500 --seed 43 --plant   # a day through the real capture path, six breaks planted
.venv/bin/payments-reconciliation reconcile --date 2026-10-20                                       # find and classify
.venv/bin/payments-reconciliation resolve --date 2026-10-20                                         # fix what can be fixed from the report
.venv/bin/payments-reconciliation resolve --date 2026-10-20                                         # again: "already resolved", nothing changes
.venv/bin/payments-reconciliation report --date 2026-10-20                                          # the daily report
.venv/bin/payments-reconciliation worklist                                                          # what needs a person
```

The same through HTTP (`make serve`, then the day's file must be in `SETTLEMENT_DIR`):

| Method and path | What it does |
| --- | --- |
| `POST /v1/days/{date}/reconcile` | Reconcile one day: the run's numbers, its breaks, and the carried differences that cleared. Safe to repeat. |
| `POST /v1/days/{date}/resolve` | Resolve what can be resolved; every resolution reports `resolved`, `already resolved`, `carried to the next report` or `needs a human`. |
| `GET /v1/days/{date}/report` | The daily report as JSON, with its text. |
| `GET /v1/worklist` | The breaks that need a person. |
| `GET /health`, `GET /health/live` | Dependency health (`503` when the database is down); process liveness. |

Errors have one shape: `{"error": {"code", "message"}}` (`settlement_missing` 404, `report_not_available` 404, `settlement_format` 422 with the line number, `invalid_request` 400).

## The SQL

Every statement the project runs against PostgreSQL is a file under [sql/](sql/), run in this order:

| File | Kind | What it holds | Run by |
| --- | --- | --- | --- |
| [001_schema.sql](sql/001_schema.sql) | DDL | `accounts`, `payments` (with the provider's `provider_txn_id`, deliberately not unique), `journal_entries`, `postings`, `refunds`, `reconciliation_runs`, `reconciliation_breaks`; keys, indexes, `CHECK` constraints; the deferred trigger that makes an unbalanced entry impossible; the triggers that make the ledger append-only | `make up` (`make db`) |
| [002_seed.sql](sql/002_seed.sql) | DML | the four accounts and a small day (2026-10-19): six captured payments with their six postings each | `make up` (`make db`) |
| [003_queries.sql](sql/003_queries.sql) | queries | trial balance, the zero-sum check, captured payments per day, duplicate provider ids, payments without their capture rows, breaks by cause and status, the worklist, timing differences still waiting, each run's first-pass numbers, what the resolutions posted | `make queries` (also at the end of `make run`) |
| [migrations/001_payments_and_ledger.sql](sql/migrations/001_payments_and_ledger.sql), [002_reconciliation.sql](sql/migrations/002_reconciliation.sql), [003_resolutions.sql](sql/migrations/003_resolutions.sql) | DDL (the lesson's steps) | step 1 both sides (payments and the ledger); steps 2 and 3 match and classify (runs and breaks); steps 4 and 5 resolve (fee corrections, reversals, refunds, a payment inserted from the report) | `make migrate` (to a scratch database); the integration test proves the result equals `001_schema.sql` |
| [reset.sql](sql/reset.sql) | DDL (outside the run order) | drop the schema; the ledger is append-only, so a reset cannot be a `DELETE` | `make db`; the test fixtures |

The provider's files are CSV: [data/settlement_2026-10-19.csv](data/settlement_2026-10-19.csv) is the sample for the seeded day; `make run` writes the 500-row days to `var/settlement/` (git-ignored).

## Done when: the lesson's checklist and the proof for each line

| # | Done when (from the lesson) | Command | What proves it |
| --- | --- | --- | --- |
| 1 | You have a day of your own records and a matching settlement file to reconcile against. | `make run` (section 1) | `simulated 2026-10-18: 500 payments recorded in the ledger, 500 rows in the settlement file`, then the path of the file. The ledger side is produced by the real capture path (`RecordCapture`: a provider call, then the payment and its six postings in one transaction); the file is the provider's own record of the same captures, so neither side is a hand-written fixture. The same seed gives the same day (`tests/unit/application/test_lesson_scenario.py`). |
| 2 | A clean day reconciles with zero breaks, and the match uses the stable id, not amount and time. | `make run` (section 1) | `reconcile: 500 of 500 matched on the provider's transaction id, 0 breaks found`. `tests/unit/domain/test_reconciliation.py::test_the_join_is_on_the_provider_id_not_on_amount_and_time` pairs a payment and a settlement row of identical amount and time under different ids and shows them as two breaks, not one match. |
| 3 | Your system finds all six and labels each with a cause, not just "mismatch". | `make run` (section 2) | `breaks found: 6`, then `ptx_0050 timing`, `ptx_0112 fee_discrepancy`, `ptx_0200 missing_capture`, `ptx_0215 duplicate_on_our_side`, `ptx_0300 provider_only_refund`, `ptx_0400 amount_mismatch`, and `6 breaks found, 6 classified`. The program exits non-zero if any id has another cause. The six are planted by their causes through the real path (see ARCHITECTURE.md), not by editing files. |
| 4 | After processing, fees are posted and timing differences clear on the day they settle, with the ledger still balanced. | `make run` (sections 3 and 8) | `ptx_0112: fee corrected, +0.18 posted, ledger balanced` (one entry of two rows). `ptx_0050` stays `CARRIED`; the next day's file contains it and `cleared carried differences: ptx_0050` prints with no action taken; `ledger sum across every posting: 0.00`. |
| 5 | Each is resolved to a correct ledger, and a test shows resolving the same break twice changes nothing. | `make run` (sections 3 and 4) | `ptx_0200: inserted from settlement report, postings added, ledger balanced`; `ptx_0215: duplicate reversed, one capture retained, ledger balanced`; `ptx_0300: refund posted from settlement report, ledger balanced`. The second pass prints `already resolved` for each and the ledger digest (a SHA-256 over every posting) is identical before and after. `tests/unit/application/test_lesson_scenario.py` and `tests/integration/test_reconciliation_on_postgres.py` assert it, and eight threads resolving one break at once post one correction. The provider is never called again (`executed` counters are checked). |
| 6 | The report balances matched money to the provider's settled total, and the worklist contains only breaks that truly need a person. | `make run` (section 6) | The report prints `Matched at the first pass: 494 transactions`, the auto-resolved and resolved sections, `Worklist (needs a human): ptx_0400 (amount_mismatch: ledger 95.00, settlement 99.99)` and a `Balance:` line that ends `[balances]`: the settled total equals clean + fee-corrected + resolved + worklist, bucket by bucket. `Difference against the ledger for these transactions: 4.99 USD` is the unexplained money, all of it `ptx_0400`. |

The scenario also runs as an end-to-end test (`tests/e2e`, through the program and a real server process, which serves the same report at `GET /v1/days/2026-10-20/report`).

## Verification status

Verified on 2026-10-27 in a fresh clone, one stack at a time: `make install`, `make check` and `make audit` (142 unit tests, 2 import contracts kept, 99% coverage of domain and application, no known vulnerabilities), `make up`, `make run` (the planted day, six breaks, resolution twice, the report, the next day; the last line is `ledger sum across every posting: 0.00`), `make queries`, `make test-integration` (33 passed), `make test-e2e` (2 passed), `make down`, and `make docker-build`. Re-run on 2026-11-02 after the technology notes were rewritten, one stack at a time: `make check` (142 unit tests, lint, types and import contracts clean), then with the database up `make test-integration` (33 passed), `make test-e2e` (2 passed), `make reconcile-sample` (one `fee_discrepancy`, `ptx_s0002`), `make migrate`, `make queries`, `make run` (ending `ledger sum across every posting: 0.00`) and `make down`.

## Stretch goals

| Stretch (from the lesson) | Built? |
| --- | --- |
| Handle multi-currency settlement and reconcile in both the transaction and the settlement currency. | No. Money carries a currency and the job is configured for one (`CURRENCY`); a currency difference would show as an amount mismatch. ARCHITECTURE.md, "How to extend it", says what to add. |
| Add an alert when breaks of a given cause exceed a threshold. | No. The counts per cause and status are one query (`make queries`, section 6); no notifier is wired. |
| Reconcile fees against the provider's fee schedule and flag an overcharge. | No. The job corrects a fee to what the provider settled; it does not judge whether that fee was right. |

## What is simplified for teaching

Only what the lesson scopes out, plus where this build differs from the lesson's text:

- **The provider is a stub.** It captures and refunds with idempotency keys, closes one batch a day at a cutoff (23:00 UTC), and can settle a fee or an amount other than the one it captured. A real provider has more statuses, files and cutoffs.
- The lesson's `payments` table is `id, provider_txn_id, amount, fee, state, captured_at`; here amounts are integer minor units with a currency, and the ledger is the double-entry one from the idempotent-payments project (the same append-only, balanced-pair rules), so every resolution is a balanced entry.
- The lesson's report row for a refund has the capture's id and a negative amount; the matcher keys on `(id, status)` so a capture can match while its refund is still a break. The lesson's `classify` sketch checks a few cases that cannot occur together; the rules here are in [domain/reconciliation.py](src/payments_reconciliation/domain/reconciliation.py).
- **Timing** is modeled as the provider's batch cutoff: a capture after 23:00 UTC settles in the next day's file. The lesson plants it as "settles the next calendar day".
- The missing capture in the missing-capture break is inserted with the start of the business day as its capture time (the report has no capture time); the fee-corrected payment's recorded fee follows the provider's.
- One currency, one process, no authentication on the API, a throwaway local database password.
- **The database is disposable.** `docker-compose.yml` runs PostgreSQL with `fsync`, `synchronous_commit` and `full_page_writes` off, so the tests and demos do not wait on disk and a crash recovers at once. Never use those settings for data you want to keep.
- The `Dockerfile` packages the API (non-root, pinned base image, the health command as its check; mount the files at `/data/settlement`) and is built by `make docker-build`; `docker compose --profile app up --build` starts it beside PostgreSQL.

## Project structure

```text
src/payments_reconciliation/
  domain/            money, fees, the chart of accounts, ledger entries that cannot be unbalanced (capture, reversal,
                     refund, fee correction), the ledger payment, settlement rows and the CSV format, and the pure
                     matching and classification rules. Depends on nothing outside the standard library.
  application/       use cases (record a capture, reconcile a day, resolve breaks, the daily report, the worklist,
                     health) and the ports they need (PaymentProvider, SettlementFiles, UnitOfWork and its repositories,
                     Clock, IdSource). Depends on domain only.
  infrastructure/    adapters: psycopg pool, unit of work and repositories; settlement files on disk; the stub provider;
                     the day simulator; settings (environment, validated); logging; retry; clock. Depends on application and domain.
  interfaces/        the command-line program, the FastAPI application and its server entry point, the health command
                     and the composition root. Wire adapters into use cases. Nothing depends on it.
sql/                 001_schema, 002_seed, 003_queries, reset, migrations/001..003.
data/                the sample settlement file for the seeded day.
tests/
  unit/              domain (each cause on its own), application (the whole scenario on an in-memory unit of work),
                     infrastructure, HTTP and the program. No services.
  integration/       the schema's rules, the migrations against the schema, the resolvers and their race, on PostgreSQL.
  e2e/               the lesson's scenario through the program and a real server process, graceful shutdown.
docs/adr/            decision records: the stable join and the causes, resolving from the report, planting by cause, timing.
tech/                one beginner-level note per technology used here (tech/README.md is the index).
docker-compose.yml   PostgreSQL 18 (and the API under the "app" profile).      Dockerfile  the API as an image.
pyproject.toml       project metadata and every tool's configuration (ruff, mypy, pytest, import-linter).
requirements*.lock   hash-pinned dependencies (runtime, and runtime plus development tools); `make lock` regenerates them.
Makefile             the only commands a reader needs (make help).
```

The dependency rule (domain depends on nothing; application on domain; infrastructure and interfaces on both; nothing on interfaces) is enforced by `make lint` through import-linter. A second contract forbids `domain` and `application` from importing `psycopg`, the web framework or `httpx`.

## Author and license

Written by Sumit Srivastava. Released under the MIT License, see [LICENSE](LICENSE). Changes are in [CHANGELOG.md](CHANGELOG.md).
