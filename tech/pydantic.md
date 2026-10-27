# Pydantic

## What it is

Pydantic is a Python library that **validates data** described by type hints. You write a class whose fields have types and limits; Pydantic checks incoming data against it and either gives you a clean object or a list of exactly what is wrong. FastAPI uses it under the hood to check every request.

## Why this repository uses it

Not directly. This repository never imports `pydantic` in its own code. Pydantic is here because **FastAPI depends on it**: when a route is declared with `business_date: date`, it is Pydantic that turns the text `2026-10-20` from the URL into a Python `date` and rejects `tomorrow`. The rest of the checking is done by the repository's own domain code (`Money`, `SettlementRow`, `parse_csv`), which uses only the standard library, so the rules of money and matching do not depend on a web library. An import-linter contract even forbids `pydantic` in the domain and application layers.

## Where it appears

| Path | Role |
| --- | --- |
| `requirements.lock` | `pydantic==2.14.1` and `pydantic-core==2.50.1` (the Rust engine that does the checking), pulled in by FastAPI. |
| `pyproject.toml`, `[tool.mypy]` | `plugins = ["pydantic.mypy"]`: teaches mypy how Pydantic-based code is typed. |
| `pyproject.toml`, import-linter contract | `pydantic` is in the list of modules `domain` and `application` may not import. |
| `src/.../interfaces/http/app.py` | Indirect: FastAPI validates `business_date: date` with Pydantic, and the app handles the resulting `RequestValidationError`. |

## How it works underneath

When FastAPI sets up a route it reads the function's parameters and type hints once and asks Pydantic to build a **validator** for each: a compiled description of the rules, executed by `pydantic-core`. On a request, FastAPI calls the validators. A success gives the converted Python value; a failure raises `ValidationError` carrying a list of errors, each with a location (`loc`), a type and a message. FastAPI wraps it as `RequestValidationError`.

By default Pydantic is in **lax mode**: it converts when it sensibly can (the text `"2026-10-20"` becomes a `date`).

## Walkthrough of this repository's use

### A path parameter checked by Pydantic

```python
@app.post("/v1/days/{business_date}/reconcile")
def reconcile(business_date: date, request: Request) -> JSONResponse:
```

`business_date: date` is the whole "schema". A good value reaches the function as a `datetime.date`; a bad one never reaches it.

### Turning its error into this API's error

```python
@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
    problems = "; ".join(f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in error.errors())
    return _error("invalid_request", problems, 400)
```

`error.errors()` is the list Pydantic produced. For `/v1/days/tomorrow/reconcile` it contains one item whose `loc` is `("path", "business_date")` and whose `msg` is "Input should be a valid date or datetime, input is too short". The handler joins them as `path.business_date: Input should be ...` and answers **400** with the code `invalid_request`.

### The mypy plugin and the import rule

```toml
[tool.mypy]
plugins = ["pydantic.mypy"]
```

The plugin lets mypy understand code that uses Pydantic models. This repository has none of its own, so the line costs nothing; it keeps the project's template consistent with the sibling repositories, which do define models.

```toml
forbidden_modules = ["psycopg", "psycopg_pool", "fastapi", "starlette", "uvicorn", "httpx", "pydantic"]
```

Part of the import-linter contract that keeps the domain and application layers free of web, database and validation libraries.

### The validation of data that is not HTTP

The settlement CSV is checked by hand in `domain/settlement.py` (`SettlementRow.__post_init__`, `parse_csv`), with errors that name the line number. That is a deliberate choice: the same rules apply whether the report comes from a file, a test string or the simulator, and the domain stays free of Pydantic.

## Try it yourself

1. Send a bad date through the app's validation without a server:

   ```bash
   .venv/bin/python - <<'EOF'
   from types import SimpleNamespace
   from fastapi.testclient import TestClient
   from payments_reconciliation.interfaces.http.app import create_app
   stand_in = SimpleNamespace(close=lambda: None)  # the app only needs something with close()
   with TestClient(create_app(lambda: stand_in), raise_server_exceptions=False) as client:
       print(client.post("/v1/days/tomorrow/reconcile").json())
   EOF
   ```

   Expected: a one-line deprecation warning from Starlette's test client (harmless), then `{'error': {'code': 'invalid_request', 'message': 'path.business_date: Input should be a valid date or datetime, input is too short'}}`. (The stand-in object replaces the real container; the request is rejected before any use case runs.)

2. See the engine's version: `.venv/bin/pip show pydantic | grep -i "^version"`. Expected: `Version: 2.14.1`.

3. Check that no repository code imports it: `grep -rn "import pydantic\|from pydantic" src tests`. Expected: no output.

## Mistakes people make

- **Assuming a library in `requirements.lock` is used directly.** `pip show pydantic` lists `Required-by: fastapi`.
- **Depending on lax conversion for money.** The domain parses amounts exactly with `Decimal`; no float ever appears.
- **Using Pydantic v1 examples.** Version 2 (pinned here) renamed several APIs (`ConfigDict`, `model_config`).
- **Putting business rules in the HTTP layer.** Pydantic checks shape; the rules live in the domain.

## Check yourself

1. Does this repository define any Pydantic models? *No; Pydantic is used indirectly through FastAPI.*
2. Who converts `2026-10-20` in the URL to a `date`? *Pydantic, through FastAPI.*
3. What status does a bad date get here, and why not 422? *400; the app maps validation errors to 400 so that 422 means "malformed provider file".*
4. Why is `pydantic` in the forbidden list for `domain`? *So the rules stay independent of web libraries.*
5. What engine runs the checks? *`pydantic-core`, written in Rust.*

## Further reading

- Pydantic 2.14 documentation: https://docs.pydantic.dev/2.14/
- Validation errors: https://docs.pydantic.dev/latest/errors/validation_errors/
- Strict vs lax mode: https://docs.pydantic.dev/latest/concepts/strict_mode/
- FastAPI and request validation: https://fastapi.tiangolo.com/tutorial/path-params/
