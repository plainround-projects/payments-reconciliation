# pytest

## What it is

pytest is the Python **test runner**. You write functions whose names start with `test_`; pytest finds them, runs each one, and reports which passed or failed. A test **fails** when it raises an exception, usually from a plain `assert` statement that is false. pytest also provides **fixtures** (reusable setup code), **markers** (labels on tests), and **parametrization** (one test body run with many inputs).

## Why this repository uses it

The lesson's promises (the join is on the provider's id, each break gets its own cause, a resolver run twice changes nothing, the report's buckets add up to the settled total, the ledger always sums to zero) are claims about behavior. Each becomes a test, in three layers: **unit** (no services; pure functions and in-memory fakes), **integration** (the real PostgreSQL) and **end-to-end** (the real command-line program and a real server process). `make test`, `make test-integration` and `make test-e2e` run them, and CI repeats them.

## Where it appears

| Path | Role |
| --- | --- |
| `tests/unit/` (142 tests) | `domain/` (matching, settlement CSV, money, ledger), `application/` (the lesson scenario and the report with in-memory storage), `infrastructure/`, `interfaces/` (HTTP and the command-line program). |
| `tests/integration/` | The schema's rules, and the reconcile-and-resolve flow on PostgreSQL. |
| `tests/e2e/` | The `demo` program and a server process. |
| `tests/conftest.py`, `tests/unit/conftest.py`, `tests/integration/conftest.py`, `tests/e2e/conftest.py` | Fixtures shared by the tests below them. |
| `pyproject.toml`, `[tool.pytest.ini_options]` | Settings. |
| `Makefile` | The `test*` targets. |

In total 177 tests are collected (`pytest tests --collect-only -q`).

## How it works underneath

1. pytest **collects**: it walks `testpaths`, imports each `test_*.py` file and finds functions named `test_*`.
2. For each test it looks at the parameter names. A parameter named like a **fixture** is filled by calling that fixture function. Fixtures can depend on other fixtures.
3. It runs the test. Python's `assert` is rewritten by pytest so that a failure shows the values on both sides.
4. It reports `.` pass, `F` fail, `E` error in setup, `s` skip. Exit status 0 means all passed.
5. A fixture's **scope** says how often it is rebuilt: per test (default), per module or per session.

## Walkthrough of this repository's use

### Configuration

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
filterwarnings = ["ignore:Using `httpx` with `starlette.testclient` is deprecated"]
markers = [
    "integration: needs PostgreSQL from docker-compose.yml (make up)",
    "e2e: runs the lesson's scenario through the command-line program and a real server against PostgreSQL (make up)",
]
```

- `testpaths`: where to look when no path is given.
- `addopts`: options always added. `-ra` prints a short summary of all non-passing tests; `--strict-markers` makes an unregistered marker an error, so a typo such as `@pytest.mark.integraton` cannot silently skip selection.
- `filterwarnings`: hides one known deprecation warning from Starlette's test client.
- `markers`: registers the two custom labels with a description.

(The `[tool.pytest.ini_options]` table is the long-standing way to put pytest settings in `pyproject.toml`; pytest 9 also reads a native `[tool.pytest]` table, but this form still works.)

### Selecting layers

```make
test: ...
	$(PY) -m pytest tests/unit --cov=payments_reconciliation --cov-report=term
test-integration: ...
	$(PY) -m pytest tests/integration -m integration
test-e2e: ...
	$(PY) -m pytest tests/e2e -m e2e
```

`-m integration` runs only tests marked `integration`. Files set `pytestmark = pytest.mark.integration` (or `e2e`) at the top, which marks every test in the file.

### A pure unit test: no fixtures at all

The matching rules are pure functions, so their tests are plain:

```python
def test_the_join_is_on_the_provider_id_not_on_amount_and_time():
    # Same amount, same minute, different ids: a fuzzy join would pair them; the stable id does not.
    result = run([payment("ptx_0001")], [settled("ptx_0002")])
    assert causes(result) == {"ptx_0001": Cause.LEDGER_ONLY_UNEXPLAINED, "ptx_0002": Cause.MISSING_CAPTURE}
```

`payment(...)`, `settled(...)` and `run(...)` are small helper functions at the top of the test file that build a ledger payment, a settlement row and call `reconcile`. The test name states the rule in a sentence, the comment says why the case matters, and one assertion checks the cause of each id.

```python
def test_a_capture_after_the_cutoff_that_is_not_in_the_report_is_a_timing_difference():
    result = run([payment("ptx_0050", at=LATE)], [])
    assert causes(result) == {"ptx_0050": Cause.TIMING}
    assert result.breaks[0].handling is Handling.CARRY
```

`LATE` is a fixed time (23:40 UTC) after the 23:00 cutoff. The test uses fixed dates and a fixed clock, so it gives the same result every day.

### Fixtures and `conftest.py`

```python
@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()

@pytest.fixture(scope="session")
def database(settings: Settings) -> None:
    wait_until_ready(settings)
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as connection:
        load_schema(connection)
```

A file named `conftest.py` is read automatically; its fixtures are available to every test in that folder tree. `scope="session"` builds `settings` and `database` once per run. `wait_until_ready` retries for up to 180 seconds, because the container may still be starting (or recovering) when the tests begin. In `tests/integration/conftest.py` the schema fixture is stricter:

```python
@pytest.fixture(autouse=True)
def _schema(settings: Settings) -> None:
    wait_until_ready(settings)
    with psycopg.connect(postgres.conninfo(settings.postgres), autocommit=True) as admin:
        load_schema(admin)
```

`autouse=True` applies it to every test in the folder, and with the default per-test scope it **reloads the schema before every test**, so no test sees another's payments. (That fixed a real failure: tests were polluting each other.)

A **yield fixture** (`flow`) builds a connection pool and the real adapters, hands them to the test with `yield`, and closes the pool afterwards. `tmp_path` is a built-in fixture that gives each test its own empty temporary folder, used here for the settlement files.

### End-to-end: one run, many assertions

```python
@pytest.fixture(scope="module")
def scenario(tmp_path_factory, database):
    folder = tmp_path_factory.mktemp("settlement")
    env = {**os.environ, "SETTLEMENT_DIR": str(folder)}
    completed = subprocess.run([sys.executable, "-m", "payments_reconciliation.interfaces.cli", "demo"],
                               env=env, capture_output=True, text=True, timeout=3000, check=False)
    return env, completed
```

The slow lesson scenario (500 simulated payments with six planted breaks) runs **once per module** as a child process, into a temporary settlement folder. The tests then assert on its output (`"breaks found: 6"`, `"ptx_0112: fee corrected, +0.18 posted, ledger balanced"`, `"ledger sum across every posting: 0.00"`) and start a real server process on a free port to check the report endpoint, the worklist and the 404 for a day with no file. The server is sent `SIGTERM` and the test asserts exit status 0.

### Expecting an exception

`pytest.raises(SomeError)` passes only if that exception is raised; the schema tests use `pytest.raises(errors.CheckViolation)` to show that the database refuses an unbalanced entry.

## Try it yourself

1. Run the fast tests:

   ```bash
   make test
   ```

   Expected: `142 passed` and a coverage table, then no error from the 95 percent check.

2. List what exists without running it:

   ```bash
   .venv/bin/python -m pytest tests --collect-only -q | tail -3
   ```

   Expected: ends with `177 tests collected`.

3. Run one test by name:

   ```bash
   .venv/bin/python -m pytest tests/unit -q -k "join_is_on_the_provider_id"
   ```

   Expected: `1 passed` (and the rest deselected).

4. See the markers at work: `.venv/bin/python -m pytest tests/unit -q -m integration` runs nothing (`142 deselected`), because unit tests carry no `integration` marker.

5. With `make up`: `make test-integration`, then `make test-e2e` (the latter runs the whole scenario once and takes the longest).

## Mistakes people make

- **Tests that depend on each other's leftovers.** The integration tests reload the schema before each test.
- **Asserting too little.** A test that only checks "no exception" proves nothing; these assert causes, statuses, row counts and sums.
- **Unregistered markers.** `--strict-markers` turns the typo into an error.
- **Real clocks in tests.** The domain functions take their time and cutoff as arguments; the application tests use a fixed clock.
- **Running integration tests without the database.** They wait up to three minutes and then error; run `make up` first.

## Check yourself

1. What makes a function a test? *Its name starts with `test_` in a `test_*.py` file.*
2. What does `scope="module"` mean for the `scenario` fixture? *It runs once per test file and its result is shared by the tests in that file.*
3. Why does the integration `_schema` fixture reload the schema for every test? *So tests cannot see each other's data.*
4. How do you run only the integration tests? *`pytest -m integration`, as `make test-integration` does.*
5. What does `tmp_path` give a test? *A fresh temporary folder.*

## Further reading

- pytest 9.1 documentation: https://docs.pytest.org/en/9.1.x/
- Fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- Marks: https://docs.pytest.org/en/stable/how-to/mark.html
- Temporary files (`tmp_path`): https://docs.pytest.org/en/stable/how-to/tmp_path.html
- Configuration reference: https://docs.pytest.org/en/stable/reference/customize.html
