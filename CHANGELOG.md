# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Drawn architecture and flow figures in `docs/diagrams/`, light and dark, shown in the README.

## [0.1.0] - 2026-10-27

### Added

- PostgreSQL 18 in `docker-compose.yml`; the API as an optional container (profile `app`).
- SQL scripts: the schema (payments carrying the provider's transaction id, an append-only double-entry ledger, refunds, daily runs and breaks), the seed (a small sample day), the lesson's queries, a reset script, and three stepwise migrations that build the same schema.
- A stub payment provider that settles in daily batches and can settle a different fee or amount than it captured; the capture path (`RecordCapture`); a day simulator that plants the lesson's six breaks by cause (a late capture, a surcharge, a process that died after the provider captured, a retry that recorded twice, a refund the service never heard about, an adjusted amount).
- The matching job: a join on the provider's stable transaction id, a classification into eleven causes, breaks stored per day and cause, timing differences carried until they settle or escalate.
- Resolvers that fix a fee, a missing capture, a duplicate and a provider-only refund from the report's own data with balanced entries, once each, never by calling the provider again.
- The daily report with a balance proof, a worklist of the breaks only a person can decide, an HTTP API, and a command-line program with the lesson's scenario (`make run`).
- Unit tests, integration tests against PostgreSQL, end-to-end tests against the program and a real server process; ruff, mypy, import-linter, hash-pinned lock files and a CI workflow.
