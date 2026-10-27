# httpx

## What it is

`httpx` is a Python library for **making** HTTP requests: you give it a URL, a method and optional headers and a body, and it returns a response object. (FastAPI and uvicorn receive requests; httpx sends them.) This repository uses its simple one-shot form, `httpx.get(url)` and `httpx.post(url)`.

## Why this repository uses it

The reconciliation job is also offered as a small HTTP API (reconcile a day, read its report, read the worklist), so two programs need to call it: the container **health check**, and the **end-to-end tests** that start a real server process and talk to it like a client would.

## Where it appears

| Path | Use |
| --- | --- |
| `src/payments_reconciliation/interfaces/health_cli.py` | `httpx.get(.../health, timeout=5.0)`; the command behind the Docker `HEALTHCHECK`. |
| `tests/e2e/conftest.py` | Polls `/health` while the server child process starts. |
| `tests/e2e/test_lesson_scenario.py` | Calls the report, worklist, reconcile and health endpoints of the running server. |
| `pyproject.toml` | Runtime dependency `httpx>=0.27,<1`; also silences a deprecation warning about Starlette's test client, which uses httpx underneath. |
| `requirements.lock` | `httpx==0.28.1`, `httpcore==1.0.9`, `h11==0.16.0`. |

## How it works underneath

An HTTP request is text over a TCP connection: a request line (`GET /health HTTP/1.1`), headers, a blank line, and an optional body. httpx builds that text, sends it with `httpcore` (the transport layer), and parses the reply: status line, headers, body. `h11` is the small library that reads and writes the HTTP/1.1 text format itself. The one-shot functions open a connection, make the request and close it.

## Walkthrough of this repository's use

### The health check

```python
host = os.environ.get("API_HOST", "127.0.0.1")
port = os.environ.get("API_PORT", "8462")
try:
    response = httpx.get(f"http://{host}:{port}/health", timeout=5.0)
except httpx.HTTPError as error:
    print(f"unreachable: {type(error).__name__}")
    return 1
print(response.text)
return 0 if response.status_code == 200 else 1
```

- The address comes from the same environment variables the server uses, so the check always looks where the server listens.
- `timeout=5.0` caps each phase (connect, read) at 5 seconds so a hung server cannot hang the check.
- `httpx.HTTPError` is the base class of all request problems (timeouts, refused connections, protocol errors). Printing only its class name keeps the output short and free of details.
- The exit code tells Docker the result: 0 healthy, 1 not. The `HEALTHCHECK` in the Dockerfile runs this program every 15 seconds.

### Starting a server and waiting for it

```python
while time.monotonic() < deadline:
    if process.poll() is not None:
        raise RuntimeError(...)
    try:
        if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
            return Server(process, port)
    except httpx.HTTPError:
        time.sleep(0.2)
```

The test starts the server process, then asks `/health` again and again (every 0.2 seconds, up to 60 seconds) until it answers 200. A refused connection just means "not up yet", so the `except` swallows it and waits. `process.poll()` returns the exit code if the server already died, which turns a silent hang into an immediate error.

### Calling the API in a test

```python
report = httpx.get(f"{server.url}/v1/days/2026-10-20/report", timeout=30).json()
assert report["balances"] is True and report["clean_transactions"] == 494
reconcile = httpx.post(f"{server.url}/v1/days/2026-10-20/reconcile", timeout=60).json()
assert httpx.post(f"{server.url}/v1/days/2020-01-01/reconcile", timeout=30).status_code == 404
```

`.json()` parses the response body into Python dicts. The last line asks for a day with no settlement file and expects `404`.

## Try it yourself

1. With the stack up and the server running (`make up`, `make serve`):

   ```bash
   make health
   ```

   Expected: `{"status":"ok","checks":{"database":"ok"}}`, exit status 0.

2. Make the failure path happen: stop the server and run `make health` again. Expected: `unreachable: ConnectError` and exit status 1.

3. Use httpx directly while the server runs:

   ```bash
   .venv/bin/python -c "import httpx; r = httpx.get('http://127.0.0.1:8462/health'); print(r.status_code, r.json())"
   ```

   Expected: `200 {'status': 'ok', 'checks': {'database': 'ok'}}`.

## Mistakes people make

- **No timeout.** Without one a stuck server can block a program forever. httpx defaults to 5 seconds, and this code sets explicit values.
- **Treating "connection refused" as a failure while a server is starting.** The test helper retries until its deadline.
- **Forgetting that an error status is a normal response.** `httpx.get` does not raise on a 404 or 503; read `status_code`.
- **Printing whole exceptions from a health check.** They can contain addresses and credentials; the check prints the class name only.

## Check yourself

1. What does `timeout=5.0` do? *Limits each phase of the request to 5 seconds.*
2. Why does the test helper catch `httpx.HTTPError` and sleep? *The server may not be listening yet; it waits and tries again.*
3. Does `httpx.get` raise an exception for a 503? *No; it returns the response, and the code checks `status_code`.*
4. What does `.json()` do? *Parses the response body as JSON into Python objects.*
5. Which exit status means "unhealthy" to Docker? *1.*

## Further reading

- httpx (0.28): https://www.python-httpx.org/
- Quickstart: https://www.python-httpx.org/quickstart/
- Timeouts: https://www.python-httpx.org/advanced/timeouts/
- Exceptions: https://www.python-httpx.org/exceptions/
