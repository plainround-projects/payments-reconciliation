# Uvicorn

## What it is

Uvicorn is an **ASGI web server**. A web server is the program that listens on a network port, accepts connections and speaks HTTP. It does not know your business rules; it hands each request to your application (here FastAPI) through a standard interface called **ASGI**, and sends the answer back. Uvicorn is a long-running process that stays up until told to stop.

## Why this repository uses it

FastAPI cannot listen on a port by itself. Uvicorn runs the API for `make serve`, the Docker image and the end-to-end tests. This repository also cares about how the server **stops**: in-flight requests must finish, the database pool must close, and the process must exit with status 0, so it customizes the shutdown.

## Where it appears

| Path | What it does with uvicorn |
| --- | --- |
| `src/payments_reconciliation/interfaces/serve.py` | `main()` builds a `uvicorn.Config` and runs `GracefulServer` (a subclass). |
| `tests/e2e/conftest.py` | Starts `python -m payments_reconciliation.interfaces.serve` as a child process, then sends it SIGTERM. |
| `pyproject.toml` | Entry point `payments-reconciliation-serve = "payments_reconciliation.interfaces.serve:main"`. |
| `Dockerfile` | `CMD ["payments-reconciliation-serve"]`. |

## How it works underneath

- Uvicorn opens a listening **socket** on `host:port`. Each client connection becomes work for an **asyncio event loop**, a single thread that switches between many waiting tasks.
- It parses HTTP/1.1, builds the ASGI request and calls the app.
- It also drives the app's **lifespan**: before accepting traffic it tells the app "startup"; on exit it says "shutdown".
- A **signal** is a small message the operating system sends a process. `SIGINT` is what Ctrl-C sends. `SIGTERM` is the polite "please stop" that Docker, Kubernetes and `kill` send first.
- On a signal, uvicorn sets `should_exit`, stops accepting new connections, waits for running requests up to `timeout_graceful_shutdown` seconds, then runs the lifespan shutdown.

## Walkthrough of this repository's use

### Configuration

```python
config = uvicorn.Config(
    app,
    host=settings.api.host,
    port=settings.api.port,
    log_config=None,
    access_log=False,
    timeout_graceful_shutdown=settings.api.shutdown_grace_seconds,
)
GracefulServer(config).run()
```

- `host`/`port`: from the environment (`API_HOST`, `API_PORT`; defaults `127.0.0.1` and `8462`; the Docker image sets `0.0.0.0` so the container is reachable).
- `log_config=None`: do not install uvicorn's own logging setup. The app installs one logfmt handler (see `logging-and-logfmt.md`) and uvicorn's messages flow through it.
- `access_log=False`: no per-request line from uvicorn, because the app's own middleware already logs each request with an id and timing.
- `timeout_graceful_shutdown`: the most seconds to wait for in-flight requests (`SHUTDOWN_GRACE_SECONDS`, default 10). After that, remaining tasks are cancelled.

### Why `GracefulServer` exists

```python
class GracefulServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
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

Uvicorn's own `capture_signals` does the same, but when it finishes it **raises the same signal again** at the process, so the process dies "by signal". That is meant to preserve the usual behavior, but to an orchestrator the exit status looks like a crash (for example `-15`). This subclass skips the re-raise: the server shuts down cleanly and `main()` returns 0.

- `signal.signal(sig, handler)` installs a handler and returns the old one, which is saved in `previous` and restored in `finally`.
- `self.handle_exit` is uvicorn's own handler; it sets `should_exit = True`.
- Signals can only be installed from the main thread, so in any other thread the method does nothing.

### In the tests

```python
process = subprocess.Popen([sys.executable, "-m", "payments_reconciliation.interfaces.serve"], env={...}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
...
server.process.send_signal(signal.SIGTERM)
output, _ = server.process.communicate(timeout=30)
```

The end-to-end tests (`tests/e2e/conftest.py`) start the real server as a child process on a free port, poll `/health` until it answers 200, call the API, then send `SIGTERM` and assert that `returncode == 0` and that the output contains `shutdown complete` and no `level=ERROR`. This is the test that would fail if `GracefulServer` re-raised the signal.

## Try it yourself

1. Start the stack and the server (two terminals):

   ```bash
   make up
   make serve
   ```

   Expected on stderr: a logfmt line `msg=listening ... host=127.0.0.1 port=8462` and `msg=started`.

2. Ask it if it is healthy:

   ```bash
   make health
   ```

   Expected: JSON such as `{"status":"ok","checks":{"database":"ok"}}` and exit status 0 (`echo $?`).

3. Press Ctrl-C in the server terminal. Expected: `msg="shutdown complete"` and the prompt returns; `echo $?` prints `0`.

4. Try a port that is busy (`API_PORT=22 make serve`): uvicorn reports it cannot bind and the process exits non-zero.

## Mistakes people make

- **Binding to `127.0.0.1` inside a container.** Nothing outside can connect. The Dockerfile sets `API_HOST=0.0.0.0`.
- **Killing with `SIGKILL`** (`kill -9`). It cannot be caught, so there is no graceful shutdown and in-flight work is cut off.
- **Letting the process die by re-raised signal.** Orchestrators report it as a failure; `GracefulServer` avoids this.
- **Ignoring the grace timeout.** If it is too short, long requests are cancelled mid-flight; if it is too long, the platform may `SIGKILL` first.

## Check yourself

1. What is ASGI? *The standard interface between an async Python web server and an app.*
2. What does `timeout_graceful_shutdown` control? *How long to wait for running requests before cancelling them at shutdown.*
3. Why subclass `uvicorn.Server`? *To stop it re-raising the signal, so the process exits with status 0 after a clean stop.*
4. Why check for the main thread? *Python allows signal handlers to be installed only from the main thread.*
5. Which signal does Docker send first on `docker stop`? *SIGTERM.*

## Further reading

- Uvicorn documentation (pinned: 0.54.0): https://www.uvicorn.org/
- Settings, including timeouts: https://www.uvicorn.org/settings/
- Python `signal`: https://docs.python.org/3.12/library/signal.html
- ASGI specification: https://asgi.readthedocs.io/en/latest/
