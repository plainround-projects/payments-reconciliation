"""Fixtures for the integration tests: a pool, the real adapters, and the stub provider behind them."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from payments_reconciliation.infrastructure import postgres
from payments_reconciliation.infrastructure.settings import Settings
from payments_reconciliation.infrastructure.simulation import SimulatedClock
from payments_reconciliation.infrastructure.system import RandomIds, SystemClock
from payments_reconciliation.interfaces.container import Container, assemble
from tests.conftest import load_schema, wait_until_ready
from tests.unit.fakes import START


@pytest.fixture(autouse=True)
def _schema(settings: Settings) -> None:
    """Every test starts from a freshly loaded schema and seed, so no test sees another's payments."""
    wait_until_ready(settings)
    with psycopg.connect(postgres.conninfo(settings.postgres), autocommit=True) as admin:
        load_schema(admin)


@pytest.fixture
def connection(settings: Settings) -> Iterator[psycopg.Connection[Any]]:
    """A plain connection (autocommit off) for tests that write SQL directly."""
    with psycopg.connect(postgres.conninfo(settings.postgres)) as connection:
        yield connection


@dataclass
class Flow:
    """The use cases on PostgreSQL, with settlement files in a temporary folder."""

    services: Container
    pool: Any


@pytest.fixture
def flow(settings: Settings, tmp_path: pathlib.Path) -> Iterator[Flow]:
    import dataclasses

    from payments_reconciliation.infrastructure.settings import ReconciliationSettings

    rules: ReconciliationSettings = dataclasses.replace(settings.reconciliation, settlement_dir=tmp_path)
    local = dataclasses.replace(settings, reconciliation=rules)
    pool = postgres.open_pool(settings.postgres)
    ids = RandomIds()
    services = assemble(
        settings=local,
        uow_factory=postgres.PostgresUnitOfWorkFactory(pool, ids),
        clock=SystemClock(),
        simulation_clock=SimulatedClock(START),
        ids=ids,
        probes={"database": postgres.PostgresHealth(pool)},
        close=pool.close,
    )
    yield Flow(services, pool)
    pool.close()
