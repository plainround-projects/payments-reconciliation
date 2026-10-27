# FastAPI

## What it is

FastAPI is a Python **web framework**: a library that turns ordinary Python functions into an HTTP API. You say "a POST to `/v1/days/2026-10-20/reconcile` runs this function", and FastAPI reads the request, checks the data, calls your function and writes the response. It is built on **Starlette** (which handles the web plumbing: routing, middleware, responses, the test client) and **Pydantic** (which checks data; see `pydantic.md`).

## Why this repository uses it

Besides the command-line program, the reconciliation job is offered as a small service: an operator or a scheduler can ask it to reconcile a day, resolve the day's breaks, fetch the day's report and read the worklist of breaks that need a person. FastAPI gives routing, path-parameter checking and clear error handling in a small amount of code, and `TestClient` lets the HTTP layer be tested without a network.

## Where it appears

| Path | What it does with FastAPI |
| --- | --- |
| `src/payments_reconciliation/interfaces/http/app.py` | `create_app()`: the application, all routes, the middleware, the three error handlers, the health endpoints. |
| `tests/unit/interfaces/test_http_and_cli.py` | Uses `fastapi.testclient.TestClient`. |
| `tests/e2e/` | Calls a real server process (with httpx). |
| `requirements.lock` | Pins `fastapi==0.162.0` and `starlette==1.10.0`. |

## How it works underneath

1. A server program (uvicorn, see `uvicorn.md`) accepts a TCP connection and parses the HTTP request.
2. It calls the FastAPI object as an **ASGI application**: a standard Python interface where the server passes the request details and the app sends back response pieces.
3. FastAPI matches the path and method to a **route** and runs the **middleware** chain around it.
4. For the matched function it reads each parameter from the right place (a name in `{braces}` in the path, the query string, a header, the JSON body) and converts and checks it using its type hint. A failure raises `RequestValidationError` before your function runs.
5. Functions defined with plain `def` (all the routes here) are run in a **worker thread**, so a slow database call does not freeze the whole server.
6. Your return value is turned into a response. Exceptions go to the registered **exception handlers**.

## Walkthrough of this repository's use

### Building the app

```python
def create_app(container_factory: Callable[[], Container]) -> FastAPI:
```

A function that builds and returns the app, rather than a global `app` object. Tests pass a factory that returns fakes; production passes one that opens the database pool.

### Startup and shutdown: the lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.container = container_factory()
    logger.info("started")
    yield
    app.state.container.close()
    logger.info("shutdown complete")

app = FastAPI(title="Payments reconciliation", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
```

A **lifespan** is one async context manager: code before `yield` runs once at startup, code after runs once at shutdown. Startup opens the database pool, shutdown closes it. `app.state` is a place to keep shared objects. `docs_url=None` and `redoc_url=None` switch off the built-in interactive documentation pages.

### Routes

```python
@app.post("/v1/days/{business_date}/reconcile")
def reconcile(business_date: date, request: Request) -> JSONResponse:
    outcome = container(request).reconcile.execute(business_date)
    return JSONResponse({"run": run_view(outcome.run), "breaks": [break_view(stored) for stored in outcome.breaks], ...})
```

- The decorator registers the function for `POST` on that path. `{business_date}` is a variable part.
- `business_date: date` is read from the path and must parse as an ISO date (`2026-10-20`). `2026-13-45` fails before the function runs and is answered with a 400.
- `request: Request` is the Starlette request object; `container(request)` reaches `request.app.state.container`, the object that holds the use cases.
- The use case does the work; the route only converts the result to JSON with small "view" functions.

The routes: `POST /v1/days/{date}/reconcile`, `POST /v1/days/{date}/resolve`, `GET /v1/days/{date}/report`, `GET /v1/worklist`, `GET /health/live`, `GET /health`.

### Middleware

```python
@app.middleware("http")
async def log_requests(request: Request, call_next: Callable[[Request], Any]) -> Response:
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    started = time.perf_counter()
    response: Response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    logger.info("request", extra=fields(request_id=request_id, method=request.method, path=request.url.path, status=response.status_code, duration_ms=...))
    return response
```

Middleware wraps every request. `await call_next(request)` runs the rest of the chain and returns the response. This one reuses the caller's request id (or makes one), times the request, adds the id to the response header and writes one log line with method, path, status and duration. It never logs a body.

### Error handlers

```python
_STATUS: dict[type[ReconciliationError], int] = {
    InvalidDateError: 400,
    InvalidMoneyError: 400,
    SettlementMissingError: 404,
    ReportNotAvailableError: 404,
    SettlementFormatError: 422,
}

@app.exception_handler(ReconciliationError)
async def named_error(request: Request, error: ReconciliationError) -> JSONResponse:
    return _error(error.code, error.message, _STATUS.get(type(error), 500))
```

Three handlers:

1. `ReconciliationError` (the program's named errors): the status comes from the table above (400 bad input, 404 no settlement file for that day or no report, 422 the file exists but is malformed), 500 if the class is not in the table. The body is `{"error": {"code": ..., "message": ...}}`.
2. `RequestValidationError`: FastAPI's default answer is 422; this app answers **400** with a readable list of `field: message` pairs, so 422 keeps one meaning: "the provider's file is malformed".
3. `Exception`: anything unexpected is logged with its traceback and answered with a generic 500, so internals never leak.

### Health endpoints

`GET /health/live` returns `{"status": "ok"}` when the process is up. `GET /health` asks the database probe and returns 200 or 503 with the checks. The Docker `HEALTHCHECK` uses `/health`.

### Testing

`TestClient(create_app(lambda: container))` calls the app directly in-process; no port is opened. Entering it with `with` runs the lifespan. The end-to-end test starts a real server process instead (see `pytest.md`).

## Try it yourself

1. Run the HTTP and command-line unit tests:

   ```bash
   .venv/bin/python -m pytest tests/unit/interfaces -q
   ```

   Expected: `12 passed` (the count grows if tests are added).

2. List the routes without starting a server:

   ```bash
   .venv/bin/python - <<'EOF'
   from payments_reconciliation.interfaces.http.app import create_app
   app = create_app(lambda: None)
   for route in app.routes:
       print(sorted(getattr(route, "methods", []) or []), route.path)
   EOF
   ```

   Expected: `['GET', 'HEAD'] /openapi.json` (FastAPI adds it; only the human-readable docs pages are switched off), then seven lines in all: `['POST'] /v1/days/{business_date}/reconcile`, `['POST'] .../resolve`, `['GET'] .../report`, `['GET'] /v1/worklist`, `/health/live` and `/health`.

3. With the stack up (`make up`, `make run`, then `make serve` in another terminal), call it:

   ```bash
   curl -s localhost:8462/v1/worklist
   curl -s -X POST localhost:8462/v1/days/2026-10-20/reconcile
   curl -i -X POST localhost:8462/v1/days/2020-01-01/reconcile
   curl -i -X POST localhost:8462/v1/days/not-a-date/reconcile
   ```

   Expected: the worklist JSON (only `ptx_0400` after the demo); the day's run and its breaks; `404` with `"code":"settlement_missing"` for a day without a file; `400` with `"code":"invalid_request"` for the bad date.

## Mistakes people make

- **Declaring a route `async def` and then calling blocking code in it.** That blocks the event loop. These routes are plain `def` so FastAPI runs them in threads.
- **Trusting the default 422** and giving it two meanings. This app maps validation errors to 400.
- **Returning raw exception text to clients.** The catch-all handler returns a fixed message.
- **Global state at import time.** Opening a database pool when the module loads makes tests and tools slow or fail; the lifespan opens it only when the app really starts.
- **Doing the work in the route.** Routes here only translate; the rules live in the use cases and the domain.

## Check yourself

1. What is the lifespan for? *Code that runs once when the app starts and once when it stops, such as opening and closing the database pool.*
2. What happens to `POST /v1/days/not-a-date/reconcile`? *The path parameter fails to parse as a date, so the validation handler answers 400.*
3. Why are the routes `def`, not `async def`? *The database driver is blocking; FastAPI runs `def` routes in a thread pool so the server stays responsive.*
4. What does 404 mean here? *There is no settlement file (or no report) for that day.*
5. What does `TestClient` avoid? *Opening a real network port; it calls the app in-process.*

## Further reading

- FastAPI documentation (0.162): https://fastapi.tiangolo.com/
- Lifespan events: https://fastapi.tiangolo.com/advanced/events/
- Handling errors: https://fastapi.tiangolo.com/tutorial/handling-errors/
- Path parameters: https://fastapi.tiangolo.com/tutorial/path-params/
- Starlette (1.10): https://www.starlette.io/
