# Python walkthrough: the interfaces package, line by line

## What it is

This note reads every line of the `interfaces` package: the composition root that wires everything together, the FastAPI application (the HTTP API), the server launcher that exits cleanly on SIGTERM, the health-check command used by Docker, and the command-line program with the lesson's demo.

An **interface** here means the way a person or another program reaches the service: HTTP requests or a terminal command. This layer contains no business rules; it only translates between the outside world and the use cases.

## Why this repository uses it

The lesson ends with a full scenario, a day with six planted differences that the system must find, classify, fix and report. The command-line `demo` runs exactly that and fails loudly if any step is wrong. The HTTP API gives the same abilities to other programs. The launcher's careful signal handling lets an orchestrator stop the service without seeing a crash.

## Where it appears

| File | What it is |
| --- | --- |
| `src/payments_reconciliation/interfaces/__init__.py` | The interfaces package. |
| `src/payments_reconciliation/interfaces/http/__init__.py` | The HTTP sub-package. |
| `src/payments_reconciliation/interfaces/container.py` | The composition root. |
| `src/payments_reconciliation/interfaces/http/app.py` | The FastAPI application. |
| `src/payments_reconciliation/interfaces/serve.py` | The server launcher with graceful shutdown. |
| `src/payments_reconciliation/interfaces/health_cli.py` | The health-check command. |
| `src/payments_reconciliation/interfaces/cli.py` | The command-line program and the demo scenario. |

## How it works underneath

**Composition root:** the one function (`assemble` or `build_container`) that creates concrete adapters and hands them to the use cases. Everything else receives its collaborators as arguments. **FastAPI** turns functions with decorators such as `@app.post("/v1/...")` into routes; it parses the path and body into typed parameters and writes the return value as JSON. **Exception handlers** convert raised errors into the one error shape. **argparse** reads the command line into an object whose attributes are the options. A **signal** is a message the operating system sends to a process; `SIGTERM` asks it to stop politely.

## Walkthrough of this repository's use



### `src/payments_reconciliation/interfaces/__init__.py` (1 lines)

```python
"""The command-line program and the HTTP API. Wire adapters into use cases; nothing depends on this layer."""
```

The docstring of the `interfaces` package: everything a person or another program touches, the command-line program and the HTTP API. It wires the adapters into the use cases. Nothing else depends on it, which is the top of the layering.

### `src/payments_reconciliation/interfaces/http/__init__.py` (1 lines)

```python
"""The FastAPI application."""
```

The docstring of the `http` sub-package: it holds the FastAPI application.

### `src/payments_reconciliation/interfaces/container.py` (84 lines)

```python
"""The composition root: build the adapters from settings and wire them into the use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from payments_reconciliation.application.daily_report import BuildDailyReport
from payments_reconciliation.application.health import CheckHealth
from payments_reconciliation.application.ports import Clock, HealthProbe, IdSource, UnitOfWorkFactory
from payments_reconciliation.application.reconcile_day import ReconcileDay
from payments_reconciliation.application.resolve_breaks import ResolveBreaks
from payments_reconciliation.application.worklist import GetWorklist
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.infrastructure import postgres
from payments_reconciliation.infrastructure.files import DirectorySettlementFiles
from payments_reconciliation.infrastructure.settings import Settings
from payments_reconciliation.infrastructure.simulation import DaySimulator, SimulatedClock
from payments_reconciliation.infrastructure.stub_provider import StubProvider
from payments_reconciliation.infrastructure.system import RandomIds, SystemClock
```

The composition root: build the adapters from settings and wire them into the use cases. This is the one place that knows which concrete classes implement which ports. `Callable` is a function type; `dataclass` writes the boring parts of a class.

```python
@dataclass(frozen=True, slots=True)
class Container:
    """The use cases the HTTP layer and the program call, and how to release resources."""

    reconcile: ReconcileDay
    resolve: ResolveBreaks
    report: BuildDailyReport
    worklist: GetWorklist
    health: CheckHealth
    simulator: DaySimulator
    uow_factory: UnitOfWorkFactory
    files: DirectorySettlementFiles
    provider: StubProvider
    close: Callable[[], None]
```

`Container` is a frozen bundle of everything the HTTP layer and the command-line program call: the five use cases (`reconcile`, `resolve`, `report`, `worklist`, `health`), the day simulator, the transaction factory, the settlement-file store, the stub provider and a `close` function that releases resources such as the connection pool.

```python
def assemble(
    *,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    clock: Clock,
    simulation_clock: SimulatedClock,
    ids: IdSource,
    probes: dict[str, HealthProbe],
    close: Callable[[], None] = lambda: None,
) -> Container:
    """Wire the use cases around the given ports. Tests call this with an in-memory unit of work."""
```

`assemble` wires the use cases around ports that are passed in, so tests can call it with an in-memory unit of work. The `*` makes all arguments keyword-only. The default `close=lambda: None` is an unnamed function that does nothing.

```python
    rules = settings.reconciliation
    files = DirectorySettlementFiles(rules.settlement_dir)
    provider = StubProvider(FeePolicy(rules.provider_fee_basis_points))
```

`rules` is the reconciliation part of the settings (currency, cutoff, carry days, settlement folder, provider fee). The files adapter reads and writes CSVs in the settlement folder; the stub provider uses a `FeePolicy` built from the configured basis points.

```python
    return Container(
        reconcile=ReconcileDay(
            uow_factory, files, clock, currency=rules.currency, cutoff=rules.cutoff, max_carry_days=rules.max_carry_days
        ),
        resolve=ResolveBreaks(uow_factory, clock),
        report=BuildDailyReport(uow_factory, files, clock, rules.currency),
        worklist=GetWorklist(uow_factory),
        health=CheckHealth(probes),
        simulator=DaySimulator(
            uow_factory, provider, simulation_clock, ids, files, cutoff=rules.cutoff, currency=rules.currency
        ),
        uow_factory=uow_factory,
        files=files,
        provider=provider,
        close=close,
    )
```

Builds the container: each use case receives its ports and settings (`ReconcileDay` also gets the currency, the cutoff and the maximum carry days; `BuildDailyReport` the currency; `DaySimulator` the provider, simulation clock, ids, files, cutoff and currency).

```python
def build_container(settings: Settings) -> Container:
    """The real wiring: a PostgreSQL pool, settlement files on disk, and the stub provider."""
    pool = postgres.open_pool(settings.postgres)
    ids = RandomIds()
    clock = SystemClock()
    return assemble(
        settings=settings,
        uow_factory=postgres.PostgresUnitOfWorkFactory(pool, ids),
        clock=clock,
        simulation_clock=SimulatedClock(clock.now()),
        ids=ids,
        probes={"database": postgres.PostgresHealth(pool)},
        close=pool.close,
    )
```

The real wiring for production and the demo: open a PostgreSQL connection pool, make random ids and the system clock, and call `assemble` with the PostgreSQL unit-of-work factory, a simulation clock that starts at the real current time, a database health probe and `pool.close` as the shutdown action.

### `src/payments_reconciliation/interfaces/http/app.py` (128 lines)

```python
"""The FastAPI application: reconcile a day, resolve its breaks, read its report and the worklist."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from payments_reconciliation.application.daily_report import ReportNotAvailableError
from payments_reconciliation.application.logs import fields
from payments_reconciliation.application.views import break_view, report_view, resolution_view, run_view
from payments_reconciliation.domain.errors import (
    InvalidDateError,
    InvalidMoneyError,
    ReconciliationError,
    SettlementFormatError,
    SettlementMissingError,
)
from payments_reconciliation.interfaces.container import Container
```

The FastAPI application: reconcile a day, resolve its breaks, read its report and the worklist. Imports: `time` and `uuid` (for timing and request ids), `asynccontextmanager` (to write the start-up/shutdown block), `AsyncIterator`, `date`, and the FastAPI classes (`FastAPI`, `Request`, `Response`, `JSONResponse`, `RequestValidationError`) plus the named errors and view functions.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
_STATUS: dict[type[ReconciliationError], int] = {
    InvalidDateError: 400,
    InvalidMoneyError: 400,
    SettlementMissingError: 404,
    ReportNotAvailableError: 404,
    SettlementFormatError: 422,
}
```

The table that maps each named error to an HTTP status: bad dates and bad money are 400; a missing settlement file and a day without a report are 404; a malformed settlement file is 422. Any other error falls back to 500 (see `named_error` below). `type[X]` means "the class X itself".

```python
def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
```

Builds the single error shape of this API: `{"error": {"code": ..., "message": ...}}` with the given status.

```python
def create_app(container_factory: Callable[[], Container]) -> FastAPI:
    """Build the application. The container is created at startup and released at shutdown."""
```

`create_app` builds and returns the application; it takes a function that makes the container of use cases (production passes the real one, tests pass fakes). Everything below is defined inside it so the routes can see the container.

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container_factory()
        logger.info("started")
        yield
        app.state.container.close()
        logger.info("shutdown complete")
```

The **lifespan**: code that runs once when the server starts (before `yield`) and once when it stops (after). At start it calls the factory, stores the container on `app.state` (a place for shared objects) and logs `started`. At stop it closes the container (the database pool) and logs `shutdown complete`. `async def`/`await` are Python's syntax for code that can pause while waiting for the network.

```python
    app = FastAPI(title="Payments reconciliation", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
```

Creates the application with a title, a version and the lifespan. `docs_url=None` and `redoc_url=None` switch off FastAPI's built-in documentation pages.

```python
    def container(request: Request) -> Container:
        result: Container = request.app.state.container
        return result
```

A small helper that fetches the container from the application's state for the current request.

```python
    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable[[Request], Any]) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        started = time.perf_counter()
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        logger.info(
            "request",
            extra=fields(
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            ),
        )
        return response
```

**Middleware** wraps every request. `log_requests` takes the caller's `X-Request-Id` header or makes a new id (`or` picks the second value when the first is empty), notes the start time, calls the rest of the chain (`await call_next(request)`), adds the id to the response header and writes one log line with the request id, method, path, status and the duration in milliseconds (`time.perf_counter()` is a precise stopwatch). It never logs a body.

```python
    @app.exception_handler(ReconciliationError)
    async def named_error(request: Request, error: ReconciliationError) -> JSONResponse:
        return _error(error.code, error.message, _STATUS.get(type(error), 500))
```

Exception handlers turn raised errors into answers. This one catches any named error and answers with its `code` and `message` and the status from the table (500 if the class is not listed).

```python
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in error.errors())
        return _error("invalid_request", problems, 400)
```

Bad request data (a date in the path that is not a date, for example) is answered with 400 and a readable text built from the list of problems: for each, the location joined with dots (`'.'.join(...)`) and the message, separated by `; `.

```python
    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.error("unexpected error", extra=fields(error=type(error).__name__), exc_info=error)
        return _error("internal_error", "an unexpected error occurred", 500)
```

The catch-all: an unexpected error is logged with its traceback (`exc_info=error`) and the client gets a fixed 500 message, so no internal detail leaks.

```python
    @app.post("/v1/days/{business_date}/reconcile")
    def reconcile(business_date: date, request: Request) -> JSONResponse:
        outcome = container(request).reconcile.execute(business_date)
        return JSONResponse(
            {
                "run": run_view(outcome.run),
                "breaks": [break_view(stored) for stored in outcome.breaks],
                "cleared_carried": [stored.finding.provider_txn_id for stored in outcome.cleared_carried],
            }
        )
```

`POST /v1/days/{business_date}/reconcile`. The part in braces becomes the parameter `business_date: date`, which FastAPI parses and checks. The answer has the run's first-pass numbers, the day's breaks and the ids of carried breaks that cleared.

```python
    @app.post("/v1/days/{business_date}/resolve")
    def resolve(business_date: date, request: Request) -> JSONResponse:
        resolutions = container(request).resolve.execute(business_date)
        return JSONResponse({"resolutions": [resolution_view(item) for item in resolutions]})
```

`POST /v1/days/{business_date}/resolve` resolves the safely resolvable breaks of the day and lists each resolution.

```python
    @app.get("/v1/days/{business_date}/report")
    def report(business_date: date, request: Request) -> JSONResponse:
        return JSONResponse(report_view(container(request).report.execute(business_date)))
```

`GET /v1/days/{business_date}/report` returns the daily report as JSON including its text rendering.

```python
    @app.get("/v1/worklist")
    def worklist(request: Request) -> JSONResponse:
        return JSONResponse({"worklist": [break_view(stored) for stored in container(request).worklist.execute()]})
```

`GET /v1/worklist` lists the breaks that need a person.

```python
    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}
```

Two health endpoints. `/health/live` only says the process is up.

```python
    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        checks = container(request).health.execute()
        healthy = all(value == "ok" for value in checks.values())
        status = "ok" if healthy else "unavailable"
        return JSONResponse({"status": status, "checks": checks}, status_code=200 if healthy else 503)
```

`/health` asks the dependencies (the database) through the `health` use case and answers 200 with `status: ok` when all are `ok`, else 503 with `status: unavailable` and the checks (`all(...)` is true when every item is true).

```python
    return app
```

`create_app` returns the finished application.

### `src/payments_reconciliation/interfaces/serve.py` (65 lines)

```python
"""`payments-reconciliation-serve`: run the API until SIGTERM or SIGINT, then exit 0."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager

import uvicorn

from payments_reconciliation.infrastructure.logging import configure_logging
from payments_reconciliation.infrastructure.settings import ConfigurationError, Settings
from payments_reconciliation.interfaces.container import build_container
from payments_reconciliation.interfaces.http.app import create_app

logger = logging.getLogger("payments_reconciliation.serve")
```

The server launcher behind the command `payments-reconciliation-serve`. `signal` lets a program catch operating-system signals; `threading` is used to check which thread runs; `uvicorn` is the web server.

```python
class GracefulServer(uvicorn.Server):
    """A uvicorn server that exits normally (status 0) after SIGINT or SIGTERM.

    uvicorn finishes in-flight requests and runs the lifespan shutdown on a signal, then raises the same signal again
    so the process dies "by signal". An orchestrator reads that as a crash; this server just returns.
    """
```

A subclass of uvicorn's server. The docstring explains the problem: on SIGINT or SIGTERM uvicorn finishes in-flight requests and runs the lifespan shutdown, and then raises the same signal again so the process dies "by signal". A platform reads that as a crash. This class makes the program exit normally with status 0.

```python
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Route SIGINT and SIGTERM to the server's own graceful exit, and put the old handlers back afterwards."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
```

`capture_signals` overrides uvicorn's method. It does nothing when not on the main thread (Python allows signal handlers only there). Otherwise it installs uvicorn's own handler (`self.handle_exit`) for SIGINT and SIGTERM with `signal.signal(sig, handler)`, which returns the old handler; the dictionary comprehension keeps the old handlers, and the `finally` block restores them. Unlike uvicorn's version it does not re-raise the signal afterwards.

```python
def main() -> int:
    """Start the server; the exit code is 0 after a graceful stop and 2 for a configuration error."""
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging(settings.log_level)
    app = create_app(lambda: build_container(settings))
    logger.info("listening", extra={"fields": {"host": settings.api.host, "port": settings.api.port}})
    config = uvicorn.Config(
        app,
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=settings.api.shutdown_grace_seconds,
    )
    GracefulServer(config).run()
    return 0
```

`main` returns the process exit status. It reads the settings (a bad one prints `configuration error: ...` and returns 2), configures logging, builds the app with a factory that creates the container, logs `listening`, and builds the uvicorn configuration: `log_config=None` leaves logging to this program, `access_log=False` because the middleware already logs requests, `timeout_graceful_shutdown` is the wait for in-flight requests. `GracefulServer(config).run()` blocks until a stop signal; then `main` returns 0.

```python
if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

The standard entry-point idiom: when the file is run as a program, exit with the status `main()` returns. `# pragma: no cover` excludes the line from coverage.

### `src/payments_reconciliation/interfaces/health_cli.py` (25 lines)

```python
"""`payments-reconciliation-health`: exit 0 when the running API reports healthy (container health check)."""

from __future__ import annotations

import os
import sys

import httpx


def main() -> int:
    """Ask `/health` of the API at API_HOST:API_PORT; print the answer and exit 0 for healthy, 1 otherwise."""
    host = os.environ.get("API_HOST", "127.0.0.1")
    port = os.environ.get("API_PORT", "8462")
    try:
        response = httpx.get(f"http://{host}:{port}/health", timeout=5.0)
    except httpx.HTTPError as error:
        print(f"unreachable: {type(error).__name__}")
        return 1
    print(response.text)
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

The command `payments-reconciliation-health`, used by the Docker health check. It reads the host and port from the same environment variables the server uses (defaults `127.0.0.1` and `8462`), asks `GET /health` with `httpx.get(..., timeout=5.0)`, prints the body, and returns 0 for status 200 and 1 for anything else. If the server cannot be reached, `httpx.HTTPError` is caught, the class name is printed and 1 is returned. `sys.exit(main())` turns the returned number into the process exit status, which Docker reads.

### `src/payments_reconciliation/interfaces/cli.py` (260 lines)

```python
"""`payments-reconciliation`: simulate a day, reconcile it, resolve its breaks, print the report, run the scenario."""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable, Sequence
from datetime import date

from payments_reconciliation.application.daily_report import render
from payments_reconciliation.application.ports import BreakStatus, StoredBreak
from payments_reconciliation.application.resolve_breaks import Outcome, Resolution
from payments_reconciliation.domain.errors import InvalidDateError, ReconciliationError
from payments_reconciliation.domain.reconciliation import Cause
from payments_reconciliation.infrastructure.logging import configure_logging
from payments_reconciliation.infrastructure.settings import ConfigurationError, Settings
from payments_reconciliation.infrastructure.simulation import DayPlan
from payments_reconciliation.interfaces.container import Container, build_container
```

The command-line program: simulate a day, reconcile it, resolve its breaks, print the report, and run the whole scenario of the lesson. New imports: `argparse` (the standard library's command-line parser), `io` and `sys` (standard input and output), and the pieces of the application the commands call.

```python
CLEAN_DAY = date(2026, 10, 18)
PLANTED_DAY = date(2026, 10, 20)
NEXT_DAY = date(2026, 10, 21)
```

Three fixed days used by the demo: a clean day, the day with six planted differences, and the following day.

```python
# The lesson's six transactions and the cause each must be classified as.
EXPECTED_CAUSES = {
    "ptx_0050": Cause.TIMING,
    "ptx_0112": Cause.FEE_DISCREPANCY,
    "ptx_0200": Cause.MISSING_CAPTURE,
    "ptx_0215": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0300": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0400": Cause.AMOUNT_MISMATCH,
}
```

The six provider transaction ids the demo plants and the cause each must be classified as. The demo checks that reconciliation finds exactly this mapping.

```python
class ScenarioError(AssertionError):
    """The system did something the lesson says it must not."""
```

An error type for "the system did something the lesson says it must not". The demo raises it with `expect` and the program prints `FAILED:` and exits with 1.

```python
def expect(condition: bool, message: str) -> None:
    """Raise `ScenarioError` with `message` unless `condition` holds."""
    if not condition:
        raise ScenarioError(message)
```

Raises `ScenarioError(message)` unless `condition` is true: the demo's own assertion, which unlike `assert` is never removed by Python's optimization flag.

```python
def parse_date(text: str) -> date:
    """An ISO date, or `InvalidDateError`."""
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise InvalidDateError(f"{text!r} is not a date; use YYYY-MM-DD") from error
```

Reads an ISO date (`2026-10-20`) with `date.fromisoformat`; a bad text becomes `InvalidDateError` that names the text (`!r` shows it with quotes).

```python
def _ledger_state(container: Container) -> tuple[str, str]:
    with container.uow_factory() as uow:
        return uow.ledger.digest(), uow.ledger.total("USD").format()
```

Reads the ledger's digest and its formatted sum in one transaction, so the demo can compare the state before and after a second resolve.

```python
def cmd_simulate(container: Container, args: argparse.Namespace) -> int:
    """Run a simulated day through the capture path and write the provider's settlement file."""
    plan = DayPlan(args.date, args.payments, args.seed, args.prefix, plant_breaks=args.plant)
    result = container.simulator.run(plan)
    print(
        f"simulated {result.business_date}: {result.recorded} payments recorded in the ledger, "
        f"{result.settlement_rows} rows in the settlement file ({result.seconds:.1f}s)"
    )
    if result.crashed_after_provider:
        print(f"  the process died after the provider captured: {', '.join(result.crashed_after_provider)}")
    if result.duplicated:
        print(f"  a retry recorded the same transaction twice: {', '.join(result.duplicated)}")
    if result.refunded_at_provider_only:
        print(f"  the provider refunded, the service never heard: {', '.join(result.refunded_at_provider_only)}")
    print(f"  settlement file: {container.files.path_for(args.date)}")
    return 0
```

`simulate-day`: builds a `DayPlan` from the arguments (date, number of payments, random seed, id prefix, whether to plant breaks), runs the simulator and prints one summary line (the f-string `{result.seconds:.1f}` shows seconds with one decimal). Optional lines say which transactions crashed after the provider captured, which were recorded twice, which the provider refunded without the service hearing, and the path of the settlement file. It returns 0.

```python
def _print_breaks(breaks: list[StoredBreak]) -> None:
    print(f"breaks found: {len(breaks)}")
    for stored in breaks:
        print(f"  {stored.finding.provider_txn_id}  {stored.finding.cause.value}")
```

Prints the number of breaks and, for each, the transaction id and cause.

```python
def cmd_reconcile(container: Container, args: argparse.Namespace) -> int:
    """Reconcile one day and list its breaks."""
    outcome = container.reconcile.execute(args.date)
    run = outcome.run
    print(
        f"{args.date}: {run.settlement_rows} settlement rows, {run.ledger_payments} payments in the ledger, "
        f"{run.clean_transactions} transactions matched cleanly"
    )
    if not outcome.breaks:
        print("0 breaks found")
    else:
        _print_breaks(outcome.breaks)
    for cleared in outcome.cleared_carried:
        print(f"  {cleared.finding.provider_txn_id}: timing difference settled, carried since {cleared.run_date}")
    return 0
```

`reconcile`: runs the day and prints how many settlement rows, ledger payments and clean matches there were, then the breaks (or `0 breaks found`), and one line for each carried difference that settled.

```python
def _resolution_line(resolution: Resolution) -> str:
    return f"{resolution.stored.finding.provider_txn_id}: {resolution.message or resolution.outcome.value}"
```

One line per resolution: the transaction id and its message (or the outcome text when the message is empty).

```python
def cmd_resolve(container: Container, args: argparse.Namespace) -> int:
    """Resolve the breaks of one day that can be resolved safely."""
    for resolution in container.resolve.execute(args.date):
        print(_resolution_line(resolution))
    return 0
```

`resolve`: resolves the day's breaks and prints one line each.

```python
def cmd_report(container: Container, args: argparse.Namespace) -> int:
    """Print the daily report."""
    print(render(container.report.execute(args.date)))
    return 0
```

`report`: prints the rendered daily report.

```python
def cmd_worklist(container: Container, args: argparse.Namespace) -> int:
    """Print the breaks that need a person."""
    items = container.worklist.execute()
    if not items:
        print("the worklist is empty")
    for stored in items:
        reason = stored.note or stored.finding.detail
        print(f"{stored.run_date}  {stored.finding.provider_txn_id}  {stored.finding.cause.value}  {reason}")
    return 0
```

`worklist`: prints `the worklist is empty` or one line per break needing a person with the run date, the transaction id, the cause and the reason (the note, or the finding's detail).

```python
def cmd_demo(container: Container, args: argparse.Namespace) -> int:
    """The lesson's scenario: a clean day, the planted day, resolution twice, the report, and the next day."""
    payments = args.payments
    print(f"== 1. A clean day ({CLEAN_DAY}): {payments} payments, nothing planted")
    cmd_simulate(container, _namespace(CLEAN_DAY, payments, 42, "ptx_a", False))
    outcome = container.reconcile.execute(CLEAN_DAY)
    expect(not outcome.breaks, f"a clean day must reconcile with zero breaks, found {len(outcome.breaks)}")
    expect(outcome.run.clean_transactions == payments, "every transaction must match on its provider id")
    matched = outcome.run.clean_transactions
    print(f"reconcile: {matched} of {payments} matched on the provider's transaction id, 0 breaks found")
    print()
    print(f"== 2. The planted day ({PLANTED_DAY}): {payments} payments and six planted breaks")
    cmd_simulate(container, _namespace(PLANTED_DAY, payments, 43, "ptx_", True))
    outcome = container.reconcile.execute(PLANTED_DAY)
    _print_breaks(outcome.breaks)
    found = {stored.finding.provider_txn_id: stored.finding.cause for stored in outcome.breaks}
    expect(found == EXPECTED_CAUSES, f"expected {EXPECTED_CAUSES}, found {found}")
    print(f"{len(found)} breaks found, {len(found)} classified (each with its own cause)")
    print()
    print("== 3. Resolve: rules for the benign ones, the report's data for the real ones")
    for resolution in container.resolve.execute(PLANTED_DAY):
        print(_resolution_line(resolution))
    print()
    print("== 4. Run every resolver a second time")
    before = _ledger_state(container)
    second = container.resolve.execute(PLANTED_DAY)
    after = _ledger_state(container)
    print(
        "   ".join(
            f"{item.stored.finding.provider_txn_id}: {item.outcome.value}"
            for item in second
            if item.outcome is Outcome.ALREADY_RESOLVED
        )
    )
    expect(before == after, "the second pass must not change the ledger")
    expect(all(item.outcome is not Outcome.RESOLVED for item in second), "nothing may be resolved twice")
    print(f"(ledger digest before {before[0][:12]}, after {after[0][:12]}: identical; ledger sum {after[1]})")
    print()
    print("== 5. Reconcile the day again: only the real break is left")
    again = container.reconcile.execute(PLANTED_DAY)
    open_statuses = {
        stored.finding.provider_txn_id: stored.status
        for stored in again.breaks
        if stored.status in {BreakStatus.NEEDS_REVIEW, BreakStatus.OPEN, BreakStatus.CARRIED}
    }
    print(f"still unresolved: {', '.join(f'{k} ({v.value})' for k, v in sorted(open_statuses.items()))}")
    expect(
        set(open_statuses) == {"ptx_0050", "ptx_0400"}, f"expected ptx_0050 (carried) and ptx_0400, got {open_statuses}"
    )
    print()
    print("== 6. The daily report")
    report = container.report.execute(PLANTED_DAY)
    print(render(report))
    expect(report.balances, "the report's buckets must add up to the settlement report's settled total")
    expect([line.provider_txn_id for line in report.worklist] == ["ptx_0400"], "the worklist must hold ptx_0400 only")
    print()
    print(f"== 7. The next day ({NEXT_DAY}): ptx_0050 settles and clears itself")
    cmd_simulate(container, _namespace(NEXT_DAY, 30, 44, "ptx_n", False))
    next_outcome = container.reconcile.execute(NEXT_DAY)
    cleared = [item.finding.provider_txn_id for item in next_outcome.cleared_carried]
    print(f"cleared carried differences: {', '.join(cleared) or 'none'}")
    expect(cleared == ["ptx_0050"], f"ptx_0050 must clear with no action, got {cleared}")
    expect(not next_outcome.breaks, f"the next day must have no breaks, found {len(next_outcome.breaks)}")
    print("worklist after the next day:")
    cmd_worklist(container, args)
    final = container.worklist.execute()
    expect([item.finding.provider_txn_id for item in final] == ["ptx_0400"], "only ptx_0400 needs a human")
    with container.uow_factory() as uow:
        total = uow.ledger.total("USD")
    expect(total.is_zero(), f"the ledger must sum to zero, got {total.format()}")
    print(f"ledger sum across every posting: {total.format()}")
    return 0
```

`demo` is the lesson's scenario in seven numbered steps, each printing what it does and checking the result with `expect`. (1) A clean day: simulate 500 payments with nothing planted, reconcile, and require zero breaks and every transaction matched. (2) The planted day: six planted breaks; reconcile and require that the found causes equal `EXPECTED_CAUSES`. (3) Resolve: print each resolution. (4) Run every resolver again: the ledger digest and sum before and after must be identical and nothing may be resolved twice. (5) Reconcile the day again: only `ptx_0050` (carried) and `ptx_0400` (needs a human) may remain unresolved. (6) The daily report: it must balance and its worklist must hold `ptx_0400` only. (7) The next day: `ptx_0050` settles and clears itself, the day has no breaks, the worklist still holds only `ptx_0400`, and the ledger sums to zero. The command returns 0 only if all checks passed.

```python
def _namespace(day: date, payments: int, seed: int, prefix: str, plant: bool) -> argparse.Namespace:
    return argparse.Namespace(date=day, payments=payments, seed=seed, prefix=prefix, plant=plant)
```

Builds the same kind of argument object the parser makes, so the demo can call `cmd_simulate` directly.

```python
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="payments-reconciliation", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    simulate = commands.add_parser("simulate-day", help="run a simulated day through the capture path")
    simulate.add_argument("--date", type=parse_date, required=True)
    simulate.add_argument("--payments", type=int, default=500)
    simulate.add_argument("--seed", type=int, default=42)
    simulate.add_argument("--prefix", default="ptx_", help="provider transaction id prefix (default ptx_)")
    simulate.add_argument("--plant", action="store_true", help="plant the lesson's six breaks")
    for name, help_text in (
        ("reconcile", "match the ledger against the day's settlement file"),
        ("resolve", "resolve the day's breaks that can be resolved safely"),
        ("report", "print the day's report"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--date", type=parse_date, required=True)
    commands.add_parser("worklist", help="list the breaks that need a person")
    demo = commands.add_parser("demo", help="the lesson's scenario, end to end")
    demo.add_argument("--payments", type=int, default=500)
    return parser
```

Builds the command-line parser. `ArgumentParser(prog=..., description=__doc__)` names the program and reuses the module docstring as its description. `add_subparsers` makes the sub-commands (`required=True` means one must be given). `simulate-day` takes `--date` (required, converted by `parse_date`), `--payments` (default 500), `--seed` (default 42), `--prefix` and the flag `--plant` (`action="store_true"` makes it true when present). A loop adds `reconcile`, `resolve` and `report`, each with a required `--date`. `worklist` takes nothing; `demo` takes `--payments`.

```python
HANDLERS: dict[str, Callable[[Container, argparse.Namespace], int]] = {
    "simulate-day": cmd_simulate,
    "reconcile": cmd_reconcile,
    "resolve": cmd_resolve,
    "report": cmd_report,
    "worklist": cmd_worklist,
    "demo": cmd_demo,
}
```

Maps each command name to its function. The type says: a function taking a container and the parsed arguments and returning an exit code.

```python
def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; exit 0 on success, 1 when the system broke a rule or the input is wrong, 2 for configuration."""
    args = _parser().parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging("WARNING" if args.command == "demo" else settings.log_level)
    container = build_container(settings)
    try:
        return HANDLERS[args.command](container, args)
    except (ReconciliationError, ScenarioError) as error:
        print(f"FAILED: {error}")
        return 1
    finally:
        container.close()
```

The entry point. `parse_args(argv)` reads the command line (with `None` it reads `sys.argv`). If standard output is a real text stream it is switched to line buffering so output appears line by line. The settings are read; a `ConfigurationError` prints `configuration error: ...` and returns 2. Logging is configured (quiet, `WARNING`, for the demo). The container is built and the handler of the chosen command runs inside `try`; a `ReconciliationError` or `ScenarioError` prints `FAILED: ...` and returns 1; the `finally` block always closes the container.

```python
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

When the file is run as a program, exit with the status `main()` returns. `# pragma: no cover` excludes the line from coverage.

## Try it yourself

1. Run the whole scenario against the database:

   ```bash
   make up
   make run
   ```

   Expected: seven numbered steps printed, ending with `ledger sum across every posting: 0.00`, and exit status 0.

2. Start the API and ask for the worklist:

   ```bash
   make serve &
   curl -s http://127.0.0.1:8462/v1/worklist
   ```

   Expected: JSON `{"worklist": [...]}`. Stop the server with `kill %1` and check it exits cleanly.

3. Ask a question that fails on purpose: `curl -s http://127.0.0.1:8462/v1/days/not-a-date/report` returns status 400 and the code `invalid_request`.

## Mistakes people make

- **Putting business rules in a route function.** Routes should call a use case and return its view.
- **Leaking internals in an error.** The catch-all handler returns a fixed message and logs the details.
- **Listening on `127.0.0.1` inside a container.** The Compose file sets the host to `0.0.0.0`.
- **Treating SIGTERM as a crash.** `GracefulServer` finishes in-flight requests and returns normally.

## Check yourself

1. What is a composition root? *The single place where concrete adapters are created and passed to the use cases.*
2. Which status does a missing settlement file give? *404, from the `_STATUS` table.*
3. Why does the demo use `expect` instead of `assert`? *`assert` can be removed by Python's optimization flag; `expect` always runs.*
4. What do exit codes 0, 1 and 2 mean in the command-line program? *Success; a broken rule or bad input; a configuration error.*
5. What does `--plant` do? *Plants the lesson's six differences in the simulated day.*

## Further reading

- FastAPI documentation: https://fastapi.tiangolo.com/
- Python 3.12 `argparse`: https://docs.python.org/3.12/library/argparse.html
- Python 3.12 `signal`: https://docs.python.org/3.12/library/signal.html
- uvicorn: https://www.uvicorn.org/
