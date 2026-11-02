# Make

## What it is

`make` is a command-line tool that runs named recipes from a file called `Makefile`. Each recipe is a **target** (a name), its **prerequisites** (things that must exist or be up to date first) and a list of shell commands. Make was designed to rebuild only what is out of date, by comparing file times. Here it is mostly used as a tidy menu of project commands: `make up`, `make run`, `make test`.

## Why this repository uses it

The lesson needs the same few steps every time: start the database, load the schema, run the scenario, test, lint, stop. One short name per step means a reader never has to remember long commands, and CI runs exactly the same commands (`make check`, `make audit`, `make test-integration`).

## Where it appears

| Path | Role |
| --- | --- |
| `Makefile` | All targets (110 lines). |
| `.github/workflows/ci.yml` | Calls `make check`, `make audit` and `make test-integration test-e2e`. |
| `README.md` | Documents the targets as the way to use the project. |

## How it works underneath

1. `make up` reads `Makefile` from the current folder and looks for the target `up`.
2. It first brings each prerequisite up to date (recursively). For a **file** target it compares timestamps: if a prerequisite is newer than the target, the recipe runs. For a **phony** target (not a file) it always runs.
3. Each recipe line is run by `/bin/sh -c` in its **own** shell, so a `cd` or a variable set on one line is gone on the next line.
4. If a command exits with a non-zero status, make stops with an error, unless the line starts with `-`.
5. Recipe lines must start with a **tab** character, not spaces (`.editorconfig` has `[Makefile] indent_style = tab` for this reason).

## Walkthrough of this repository's use

### Variables

```make
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
POSTGRES_USER ?= reconciliation
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
```

- `?=` sets the variable only if it has no value yet, so you can override it: `make PYTHON=python3.12 install`.
- `:=` evaluates the right side once, immediately.
- `$(shell ...)` runs a command and uses its output. `command -v NAME` prints the path of `NAME` if it exists; `||` tries the next one if it does not.
- `$(VAR)` inserts a variable.

### Reading `.env`

```make
-include .env
export
```

`include` pastes another file into the Makefile (here `.env` holds lines like `NAME=value`). The leading `-` means "do not fail if the file is missing". A bare `export` makes every make variable an **environment variable** of the commands it runs, so Docker Compose and the Python programs see `POSTGRES_PASSWORD` and the rest.

### Phony targets

```make
.PHONY: help install lock env up db reset-db migrate queries run serve health lint format test ...
```

Declares that these names are commands, not files. Without it, a file named `test` in the folder would make `make test` say "nothing to do".

### The self-documenting `help`

```make
help: ## list the targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

- A `## text` comment after a target is its description.
- `@` in front of a recipe line hides the command itself.
- `$(MAKEFILE_LIST)` is the list of makefiles read (`Makefile` and `.env`). `grep -h` prints matching lines without the file name; without `-h`, two files would make grep prefix each line with `Makefile:`.
- `sed` replaces `: ... ## ` with a tab, leaving `target<TAB>description`; `sort` orders them.

### A file target with prerequisites: the stamp

```make
$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	...
	touch $@

install: $(STAMP) ## create .venv from the lock file
```

`.venv/.installed` is an empty file. It is rebuilt only when a lock file or `pyproject.toml` is newer. `$@` is the name of the target. Other targets list `$(STAMP)` as a prerequisite so the environment exists first.

### Calling targets and handling errors

```make
up: env $(STAMP) ## start PostgreSQL and load sql/001_schema.sql and sql/002_seed.sql
	docker compose up -d --wait postgres
	$(MAKE) --no-print-directory db
```

- `$(MAKE)` re-runs make itself (always use it for this, never the word `make`). `--no-print-directory` stops the "Entering directory" noise.
- `run: $(STAMP) db` lists `db` as a prerequisite, so the database is rebuilt every time.
- A recipe line starting with `-` ignores its error: `-docker compose exec ... "DROP DATABASE IF EXISTS reconciliation_migrated"`.

### Shell loops inside a recipe

```make
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql ... < $$file; done
```

`$$` is how you write a single `$` for the shell (make would otherwise treat `$file` as its own variable). The backslash at the end of a line continues the same shell command on the next line.

### Recipes that prepare files, then run the program

```make
CLI := $(VENV)/bin/payments-reconciliation

run: $(STAMP) db ## the lesson's scenario end to end, including the planted breaks (resets the database)
	rm -rf var/settlement
	$(CLI) demo

reconcile-sample: $(STAMP) ## reconcile the seeded sample day against its sample settlement file
	mkdir -p var/settlement && cp data/settlement_2026-10-19.csv var/settlement/
	$(CLI) reconcile --date 2026-10-19
	$(CLI) resolve --date 2026-10-19
	$(CLI) report --date 2026-10-19
```

- `CLI` is the path of the command that `pip install -e .` created (`payments-reconciliation`, from `[project.scripts]`).
- `run` first rebuilds the database (prerequisite `db`) and empties the folder `var/settlement`, where the demo writes the provider's CSV files, so every run starts from nothing. `rm -rf` removes a folder and everything in it without asking.
- `reconcile-sample` copies the checked-in sample file into place (`mkdir -p` creates the folder and does not complain if it exists), then runs three sub-commands in order for the seeded day.
- A target without `.PHONY` trouble: both names are listed in `.PHONY`.

### Test targets

```make
test: $(STAMP)
	$(PY) -m pytest tests/unit --cov=payments_reconciliation --cov-report=term
	$(PY) -m coverage report --include='src/payments_reconciliation/domain/*,src/payments_reconciliation/application/*' --fail-under=95
```

The second line makes `make test` fail if domain and application coverage drops below 95 percent.

### `check`

`check: lint test` has no recipe; it only lists prerequisites, so it runs both. CI calls it.

<!-- listing:Makefile -->
### `Makefile` (110 lines)

```make
# make up | db | run | reconcile-sample | queries | serve | test | test-integration | test-e2e | lint | audit | down   (make help lists all)
PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
PY := $(VENV)/bin/python
STAMP := $(VENV)/.installed
POSTGRES_USER ?= reconciliation
POSTGRES_DB ?= reconciliation
PSQL := docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 -q
CLI := $(VENV)/bin/payments-reconciliation
```

The first line is a comment listing the targets (`make up`, `make db`, ...). `PYTHON ?= $(shell command -v python3.13 || command -v python3.12 || command -v python3)`: `?=` assigns only if the variable has no value yet, so you can override it on the command line; `$(shell ...)` runs a shell command and uses its output; `command -v name` prints where a program is; `||` tries the next alternative when the previous one printed nothing. So the newest of the three Pythons found is used. `VENV := .venv` names the virtual-environment folder (`:=` evaluates immediately); `PY` is the interpreter inside it; `STAMP` is a marker file `.venv/.installed`; `POSTGRES_USER` and `POSTGRES_DB` default to `reconciliation`. `PSQL` is the command that runs `psql` inside the database container (`-T` means no terminal, `-v ON_ERROR_STOP=1` stops at the first SQL error, `-q` is quiet). `CLI` is the command-line program installed into the virtual environment, `payments-reconciliation`.

```make
-include .env
export
```

`-include .env` reads the file `.env` (variables of the form `NAME=value`) into make, and the leading dash means "no error if it does not exist". A bare `export` then passes every make variable to the programs make starts, so the Python programs and Docker Compose see the password and ports.

```make
.PHONY: help install lock env up db reset-db migrate queries run reconcile-sample serve health lint format test \
        test-integration test-e2e audit check docker-build down clean
```

`.PHONY:` lists names that are commands and not files, so a file called `test` can never stop `make test` from running. The backslash continues the line.

```make
help: ## list the targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort
```

`help` prints the targets. Every target with a `## description` comment is found by `grep -hE '^[a-z-]+:.*## '` over the makefiles (`-h` hides file names, `-E` enables extended patterns), `sed` rewrites `target: ... ## text` into `target<TAB>text`, and `sort` orders them. The leading `@` stops make from printing the command itself.

```make
$(STAMP): requirements-dev.lock pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock
	$(VENV)/bin/pip install -q --no-deps -e .
	touch $@

install: $(STAMP) ## create .venv from the lock file
```

The environment target. `$(STAMP): requirements-dev.lock pyproject.toml` says the file `.venv/.installed` depends on those two files; make runs the recipe again only when either is newer. The recipe lines (each starts with a tab): create the virtual environment (`-m venv`), install exactly the locked packages (`pip install -q --require-hashes -r requirements-dev.lock`: `-q` quiet, `--require-hashes` accepts only files whose hash is in the lock), install this project itself in editable mode without resolving dependencies (`--no-deps -e .`), and `touch $@` creates the stamp (`$@` means the target name). `install` just depends on the stamp.

```make
lock: ## regenerate the hash-pinned lock files from pyproject.toml (needs uv)
	uv pip compile pyproject.toml --generate-hashes --python-version 3.12 -o requirements.lock
	uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 -o requirements-dev.lock
```

`lock` regenerates the two lock files from `pyproject.toml` with `uv pip compile ... --generate-hashes --python-version 3.12 -o ...`. The second line adds `--extra dev` for the development tools. It needs the tool `uv`.

```make
env:
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
```

`env` creates `.env` from `.env.example` only if it does not exist: `[ -f .env ] || { cp .env.example .env; echo "..."; }` (`[ -f file ]` tests that the file exists; `||` runs the next part only when the test failed).

```make
up: env $(STAMP) ## start PostgreSQL and load sql/001_schema.sql and sql/002_seed.sql
	docker compose up -d --wait postgres
	$(MAKE) --no-print-directory db
```

`up` starts the database: it needs `.env` and the environment, then `docker compose up -d --wait postgres` starts the `postgres` service in the background (`-d`) and waits until its health check passes (`--wait`), then `$(MAKE) --no-print-directory db` runs the `db` target.

```make
reset-db:
	$(PSQL) < sql/reset.sql

db: reset-db ## recreate the schema from sql/001_schema.sql and load sql/002_seed.sql (drops existing data)
	$(PSQL) < sql/001_schema.sql
	$(PSQL) < sql/002_seed.sql

# The lesson's stepwise path: apply sql/migrations/*.sql, in order, to a scratch database (reconciliation_migrated).
```

`reset-db` runs `sql/reset.sql` through `psql` (the `< file` feeds the file to its standard input). `db` depends on it, then loads the schema and the seed the same way.

```make
migrate: ## apply sql/migrations/*.sql to a scratch database and list its tables
	-docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "DROP DATABASE IF EXISTS reconciliation_migrated"
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -q -c "CREATE DATABASE reconciliation_migrated"
	@for file in sql/migrations/*.sql; do echo "applying $$file"; \
	  docker compose exec -T postgres psql -U $(POSTGRES_USER) -d reconciliation_migrated -v ON_ERROR_STOP=1 -q < $$file; done
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d reconciliation_migrated -c "\dt"
```

`migrate` applies the stepwise migrations to a scratch database: the first line drops `reconciliation_migrated` if it exists (the leading `-` ignores a failure), the second creates it, the loop `for file in sql/migrations/*.sql; do ...; done` applies each file in sorted order (`$$file` is how a shell variable is written inside a makefile), and the last line lists the tables.

```make
queries: ## run sql/003_queries.sql
	docker compose exec -T postgres psql -U $(POSTGRES_USER) -d $(POSTGRES_DB) -v ON_ERROR_STOP=1 < sql/003_queries.sql

# The lesson's whole scenario: a clean day, the planted day (500 payments, six breaks), resolution twice, the report,
# the next day. Resets the database and the settlement folder first.
```

`queries` runs `sql/003_queries.sql`.

```make
run: $(STAMP) db ## the lesson's scenario end to end, including the planted breaks (resets the database)
	rm -rf var/settlement
	$(CLI) demo
	$(MAKE) --no-print-directory queries

# Reconciles the seeded day (sql/002_seed.sql) against data/settlement_2026-10-19.csv: one fee difference.
```

`run` is the lesson's scenario. It depends on the stamp and on `db` (so the database is rebuilt first). Its recipe: `rm -rf var/settlement` removes the folder of settlement files from earlier runs, `$(CLI) demo` runs the seven-step demo, and `$(MAKE) --no-print-directory queries` runs the SQL queries afterwards. The comment above explains the scenario: a clean day, the planted day (500 payments, six breaks), resolution twice, the report and the next day.

```make
reconcile-sample: $(STAMP) ## reconcile the seeded sample day against its sample settlement file
	mkdir -p var/settlement && cp data/settlement_2026-10-19.csv var/settlement/
	$(CLI) reconcile --date 2026-10-19
	$(CLI) resolve --date 2026-10-19
	$(CLI) report --date 2026-10-19
```

`reconcile-sample` reconciles the seeded sample day against the sample provider file `data/settlement_2026-10-19.csv`, which differs only in one fee. `mkdir -p var/settlement` creates the folder if needed (`-p` makes parents and does not fail if it exists), `cp` copies the sample file into it, then the program reconciles the day, resolves its breaks and prints the report. The comment above it says what to expect: one fee difference.

```make
serve: $(STAMP) ## run the HTTP API in the foreground (Ctrl-C or SIGTERM stops it gracefully)
	$(VENV)/bin/payments-reconciliation-serve
```

`serve` runs the HTTP API in the foreground, using the command installed into the virtual environment. `health` runs the health command.

```make
health: $(STAMP) ## exit 0 when the running API reports healthy
	$(VENV)/bin/payments-reconciliation-health
```

`health` asks the running API whether it is healthy.

```make
lint: $(STAMP) ## ruff check, ruff format check, mypy, import-linter
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	$(VENV)/bin/lint-imports
```

`lint` runs, in order: `ruff check .` (style and likely bugs), `ruff format --check .` (is the layout canonical), `mypy` (types) and `lint-imports` (the layering rules). The first failure stops make.

```make
format: $(STAMP) ## apply ruff's formatting and safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

# Unit tests need no services. The domain and application must stay at 95% coverage or more.
```

`format` applies Ruff's safe fixes and formatting.

```make
test: $(STAMP) ## unit tests with coverage (no database needed)
	$(PY) -m pytest tests/unit --cov=payments_reconciliation --cov-report=term
	$(PY) -m coverage report --include='src/payments_reconciliation/domain/*,src/payments_reconciliation/application/*' --fail-under=95
```

`test` runs the unit tests with coverage (`--cov=payments_reconciliation --cov-report=term`) and then fails if the coverage of the domain and application packages is under 95 percent (`coverage report ... --fail-under=95`).

```make
test-integration: $(STAMP) ## adapters and the lesson's scenario against the running database (make up first)
	$(PY) -m pytest tests/integration -m integration
```

`test-integration` runs the tests marked `integration` against the running database; `test-e2e` runs the tests marked `e2e`, which start a real server process.

```make
test-e2e: $(STAMP) ## the program and a real server process against the running database (make up first)
	$(PY) -m pytest tests/e2e -m e2e
```

The end-to-end tests.

```make
audit: $(STAMP) ## check the locked dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit --no-deps --disable-pip -r requirements-dev.lock
```

`audit` checks the locked packages for known vulnerabilities (`pip-audit --no-deps --disable-pip -r requirements-dev.lock`).

```make
check: lint test ## everything that needs no services
```

`check` has no recipe of its own: it depends on `lint` and `test`, so it runs both. CI calls it.

```make
docker-build: ## build the container image of the API
	docker build -t payments-reconciliation:dev .
```

`docker-build` builds the API image with `docker build -t payments-reconciliation:dev .`.

```make
down: ## stop PostgreSQL and delete its data
	docker compose --profile app down -v
```

`down` stops the containers and deletes their data (`docker compose --profile app down -v`).

```make
clean: down
	rm -rf $(VENV) logs var .pytest_cache .mypy_cache .ruff_cache .coverage
```

`clean` depends on `down` and then deletes the virtual environment, logs, caches and coverage data with `rm -rf`.

<!-- /listing:Makefile -->

## Try it yourself

1. List the commands:

   ```bash
   make help
   ```

   Expected: lines such as `audit<TAB>check the locked dependencies for known vulnerabilities`, sorted by name.

2. See what a target would do without doing it:

   ```bash
   make -n test
   ```

   Expected: the two `python -m ...` commands printed, not run.

3. Watch the stamp logic: `make install` (quick if up to date), then `touch pyproject.toml && make install -n` shows the rebuild commands.

4. Override a variable: `make -n up POSTGRES_USER=other`.

## Mistakes people make

- **Spaces instead of a tab** at the start of a recipe line: `missing separator`.
- **Expecting `cd` to carry over** between recipe lines. Each line is its own shell.
- **Writing `$var` instead of `$$var`** for a shell variable.
- **Forgetting `.PHONY`**, so a same-named file blocks the target.
- **Typing `make` where `$(MAKE)` is needed** inside a recipe.

## Check yourself

1. Why does each target need a tab-indented recipe? *It is the Makefile syntax; spaces cause `missing separator`.*
2. What does `$@` mean? *The name of the target being built.*
3. What does `-include .env` do if `.env` does not exist? *Nothing; the `-` ignores the missing file.*
4. Why `$$file` in the migrate loop? *`$$` passes a literal `$` to the shell so the shell variable is used.*
5. Why does `make help` use `grep -h`? *Without it, with two files in `MAKEFILE_LIST`, each line would carry a filename prefix and the target name would be lost.*

## Further reading

- GNU Make manual (the Makefile on macOS and Linux is read by GNU Make 3.81 or newer): https://www.gnu.org/software/make/manual/
- Phony targets: https://www.gnu.org/software/make/manual/html_node/Phony-Targets.html
- Automatic variables: https://www.gnu.org/software/make/manual/html_node/Automatic-Variables.html
