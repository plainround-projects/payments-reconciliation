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
    rules = settings.reconciliation
    files = DirectorySettlementFiles(rules.settlement_dir)
    provider = StubProvider(FeePolicy(rules.provider_fee_basis_points))
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
