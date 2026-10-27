# Environment variables and the .env file

## What it is

An **environment variable** is a named text value that the operating system gives to every program it starts (for example `HOME` or `PATH`). Programs read them to find out about their surroundings. A **`.env` file** is a plain text file of `NAME=value` lines that this project's `Makefile` loads into the environment before running commands. `.env.example` is the committed template; `.env` is your private copy and is never committed.

## Why this repository uses it

The service follows the "twelve-factor" rule that **configuration lives in the environment, not in the code**. The same program must run on a laptop, in CI and in a container, only with different values: which database, which port, and above all the **password**. A secret in source code ends up in git history; a secret in the environment does not.

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../infrastructure/settings.py` | `Settings.from_env()` reads and checks every variable once, at startup. |
| `.env.example` | The template: one line per variable, with a local-development value and no real secret. |
| `.env` | Your copy, created by `make up` the first time; ignored by git and by Docker builds. |
| `Makefile` | `-include .env` and `export` pass the variables to every command. |
| `docker-compose.yml` | `${VAR:-default}` reads them to configure the containers. |
| `.github/workflows/ci.yml` | Sets `POSTGRES_PASSWORD` for the integration job. |
| `Dockerfile` | Sets `API_HOST` and `API_PORT` defaults for the container. |

## How it works underneath

When a program starts, its parent hands it a copy of a dictionary of variables. The program reads it (`os.environ`). A child process inherits a copy. A change made inside a program is not seen by its parent. A shell sets one for a single command with `NAME=value command`.

## Walkthrough of this repository's use

### Reading and checking

```python
def _text(environ: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = environ.get(name, default if default is not None else "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set and not empty")
    return value

def _positive_int(environ, name, default, *, maximum=None) -> int:
    raw = environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}")
    ...
```

Environment values are always text. These helpers fetch a name, apply a default when there is one, and refuse nonsense. `POSTGRES_PASSWORD` has **no default**, so a missing password is a startup error, not a silent empty password.

### The variables

| Name | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_HOST` | `localhost` | Database host. |
| `POSTGRES_PORT` | `54612` | Database port on the host. |
| `POSTGRES_DB`, `POSTGRES_USER` | `reconciliation` | Database and role names. |
| `POSTGRES_PASSWORD` | none | Required; `make up` copies a local-development value from `.env.example`. |
| `POSTGRES_CONNECT_TIMEOUT_SECONDS` | `5` | Connect timeout. |
| `DB_POOL_MAX_SIZE` | `10` | Largest connection pool. |
| `API_HOST`, `API_PORT` | `127.0.0.1`, `8462` | Where the API listens. |
| `SHUTDOWN_GRACE_SECONDS` | `10` | How long to wait for in-flight requests at shutdown. |
| `CURRENCY` | `USD` | The one currency this deployment reconciles; must be one the money type knows. |
| `SETTLEMENT_DIR` | `var/settlement` | The folder holding the provider's daily files, `settlement_YYYY-MM-DD.csv`. |
| `SETTLEMENT_CUTOFF_UTC` | `23:00` | The provider's batch cutoff, `HH:MM` in UTC. A capture at or after it settles the next day. |
| `MAX_CARRY_DAYS` | `2` | How many days a timing difference may wait before it becomes a break for a person. |
| `PROVIDER_FEE_BPS` | `290` | The stub provider's fee, in basis points (290 = 2.90 percent). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

### Two kinds beyond text and numbers

```python
def _clock_time(environ: Mapping[str, str], name: str, default: str) -> time:
    raw = environ.get(name, default).strip()
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be HH:MM in UTC, got {raw!r}") from error
```

`SETTLEMENT_CUTOFF_UTC` is read as `HH:MM` and turned into a `datetime.time`. `raw.split(":")` cuts `"23:00"` into two pieces; the assignment `hour, minute = ...` fails with a `ValueError` if there are not exactly two, and so does `int("xx")` or an hour of 25, so one `except` catches every bad value and reports it by name. `SETTLEMENT_DIR` becomes a `pathlib.Path`, and `CURRENCY` is checked against the currencies the money type supports.

### A frozen, validated object

`Settings` is a frozen dataclass of nested frozen dataclasses. The password field is `field(repr=False)`, so printing or logging a `PostgresSettings` never shows it.

### How `.env` reaches programs

```make
-include .env
export
```

Make includes `.env` (variables become make variables) and `export` hands them all to the commands it runs. So `make serve`, `make test-integration`, `psql` through Compose and the Python programs all see the same values. In CI there is no `.env`; the workflow sets the variables itself.

### Creating `.env`

```make
env:
	@[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
```

Copies the template only if `.env` does not exist, so your edits are never overwritten.

### Rules for secrets here

- The password in `.env.example` is for a local, throwaway database only.
- Nothing in `src/` contains a credential.
- The logger masks secret-looking field names and card-number-shaped text.
- `.env` is in `.gitignore` and `.dockerignore`.

## Try it yourself

1. See your effective settings without the password:

   ```bash
   set -a; . ./.env; set +a; .venv/bin/python -c "from payments_reconciliation.infrastructure.settings import Settings; print(Settings.from_env())"
   ```

   Expected: a `Settings(postgres=PostgresSettings(host='localhost', port=54612, ...), ...)` line with **no** password in it.

2. Trigger the missing-password error:

   ```bash
   env -u POSTGRES_PASSWORD .venv/bin/python -c "from payments_reconciliation.infrastructure.settings import Settings; Settings.from_env()"
   ```

   Expected (if the variable is not set in your shell): `ConfigurationError: POSTGRES_PASSWORD must be set and not empty`.

3. Override one value for one command: `API_PORT=9000 make serve`, then `API_PORT=9000 make health`.

4. Bad value: `LOG_LEVEL=LOUD make serve` prints `configuration error: LOG_LEVEL must be one of [...]` and exits with status 2.

## Mistakes people make

- **Committing `.env`.** It is git-ignored; commit only `.env.example`.
- **Reading variables all over the code.** Here only `settings.py` does, so the list of settings is in one file.
- **Defaulting a secret.** A default password can reach production unnoticed; here it is required.
- **Forgetting values are strings.** `"0"` is truthy as text; the helpers convert and validate.
- **Putting spaces around `=`** in a `.env` line: Make and shell parse it differently.

## Check yourself

1. Why does `POSTGRES_PASSWORD` have no default? *A missing password must fail loudly, not fall back.*
2. What does `export` do in the Makefile? *Passes every variable to the commands make runs.*
3. Which file is committed, `.env` or `.env.example`? *`.env.example`.*
4. What does `field(repr=False)` do? *Keeps the field out of the printed form of the object, so the password is not logged.*
5. Where is the list of settings read? *Only `Settings.from_env()` in `settings.py`.*

## Further reading

- The Twelve-Factor App, III. Config: https://12factor.net/config
- Python `os.environ`: https://docs.python.org/3.12/library/os.html#os.environ
- Docker Compose environment variables: https://docs.docker.com/compose/how-tos/environment-variables/
- GNU Make `include` and `export`: https://www.gnu.org/software/make/manual/html_node/Environment.html
