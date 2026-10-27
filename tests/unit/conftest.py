"""Fixtures: the service wired to in-memory storage, the stub provider and a temporary settlement folder."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import pytest

from payments_reconciliation.infrastructure.settings import Settings
from payments_reconciliation.infrastructure.simulation import SimulatedClock
from payments_reconciliation.interfaces.container import Container, assemble
from tests.unit.fakes import START, FixedClock, InMemoryDatabase, SequentialIds


@dataclass
class World:
    """Everything a test needs: the use cases, the storage to look into, the clocks."""

    services: Container
    db: InMemoryDatabase
    clock: FixedClock
    settings: Settings


def build_world(directory: pathlib.Path, *, max_carry_days: int = 2) -> World:
    settings = Settings.from_env(
        {
            "POSTGRES_PASSWORD": "not-a-real-secret",
            "SETTLEMENT_DIR": str(directory),
            "MAX_CARRY_DAYS": str(max_carry_days),
        }
    )
    db = InMemoryDatabase()
    clock = FixedClock()
    services = assemble(
        settings=settings,
        uow_factory=db.unit_of_work,
        clock=clock,
        simulation_clock=SimulatedClock(START),
        ids=SequentialIds(),
        probes={},
    )
    return World(services, db, clock, settings)


@pytest.fixture
def world(tmp_path: pathlib.Path) -> World:
    return build_world(tmp_path)
