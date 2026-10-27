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

logger = logging.getLogger(__name__)

_STATUS: dict[type[ReconciliationError], int] = {
    InvalidDateError: 400,
    InvalidMoneyError: 400,
    SettlementMissingError: 404,
    ReportNotAvailableError: 404,
    SettlementFormatError: 422,
}


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(container_factory: Callable[[], Container]) -> FastAPI:
    """Build the application. The container is created at startup and released at shutdown."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container_factory()
        logger.info("started")
        yield
        app.state.container.close()
        logger.info("shutdown complete")

    app = FastAPI(title="Payments reconciliation", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)

    def container(request: Request) -> Container:
        result: Container = request.app.state.container
        return result

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

    @app.exception_handler(ReconciliationError)
    async def named_error(request: Request, error: ReconciliationError) -> JSONResponse:
        return _error(error.code, error.message, _STATUS.get(type(error), 500))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        problems = "; ".join(f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in error.errors())
        return _error("invalid_request", problems, 400)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.error("unexpected error", extra=fields(error=type(error).__name__), exc_info=error)
        return _error("internal_error", "an unexpected error occurred", 500)

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

    @app.post("/v1/days/{business_date}/resolve")
    def resolve(business_date: date, request: Request) -> JSONResponse:
        resolutions = container(request).resolve.execute(business_date)
        return JSONResponse({"resolutions": [resolution_view(item) for item in resolutions]})

    @app.get("/v1/days/{business_date}/report")
    def report(business_date: date, request: Request) -> JSONResponse:
        return JSONResponse(report_view(container(request).report.execute(business_date)))

    @app.get("/v1/worklist")
    def worklist(request: Request) -> JSONResponse:
        return JSONResponse({"worklist": [break_view(stored) for stored in container(request).worklist.execute()]})

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        checks = container(request).health.execute()
        healthy = all(value == "ok" for value in checks.values())
        status = "ok" if healthy else "unavailable"
        return JSONResponse({"status": status, "checks": checks}, status_code=200 if healthy else 503)

    return app
