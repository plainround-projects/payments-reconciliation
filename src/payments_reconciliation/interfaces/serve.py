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


class GracefulServer(uvicorn.Server):
    """A uvicorn server that exits normally (status 0) after SIGINT or SIGTERM.

    uvicorn finishes in-flight requests and runs the lifespan shutdown on a signal, then raises the same signal again
    so the process dies "by signal". An orchestrator reads that as a crash; this server just returns.
    """

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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
