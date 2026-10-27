"""The lesson's six breaks, found, classified and resolved against the real database."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from payments_reconciliation.application.ports import BreakStatus
from payments_reconciliation.application.resolve_breaks import Outcome
from payments_reconciliation.domain.reconciliation import Cause
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants
from tests.integration.conftest import Flow

pytestmark = pytest.mark.integration

# The same six breaks as the lesson, at positions a 60-payment day reaches.
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)
SIX = {
    "ptx_0005": Cause.TIMING,
    "ptx_0008": Cause.FEE_DISCREPANCY,
    "ptx_0012": Cause.MISSING_CAPTURE,
    "ptx_0015": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0020": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0025": Cause.AMOUNT_MISMATCH,
}
DAY = date(2026, 10, 20)
NEXT = date(2026, 10, 21)


def planted(flow: Flow, day: date = DAY) -> None:
    flow.services.simulator.run(DayPlan(day, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))


def digest(flow: Flow) -> tuple[str, str]:
    with flow.services.uow_factory() as uow:
        return uow.ledger.digest(), uow.ledger.total("USD").format()


def test_a_clean_day_reconciles_with_zero_breaks(flow):
    flow.services.simulator.run(DayPlan(date(2026, 9, 1), 60, 42, "ptx_c"))
    outcome = flow.services.reconcile.execute(date(2026, 9, 1))
    assert outcome.breaks == [] and outcome.run.clean_transactions == 60


def test_the_six_planted_breaks_are_found_and_classified_by_cause(flow):
    planted(flow)
    outcome = flow.services.reconcile.execute(DAY)
    assert {b.finding.provider_txn_id: b.finding.cause for b in outcome.breaks} == SIX
    assert outcome.run.clean_transactions == 54


def test_resolving_twice_changes_nothing_the_second_time_and_the_ledger_stays_balanced(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    first = {r.stored.finding.provider_txn_id: r.outcome for r in flow.services.resolve.execute(DAY)}
    assert first["ptx_0008"] is first["ptx_0012"] is first["ptx_0015"] is first["ptx_0020"] is Outcome.RESOLVED
    assert first["ptx_0005"] is Outcome.CARRIED and first["ptx_0025"] is Outcome.NEEDS_REVIEW
    before = digest(flow)
    second = {r.stored.finding.provider_txn_id: r.outcome for r in flow.services.resolve.execute(DAY)}
    assert digest(flow) == before and before[1] == "0.00"
    assert second["ptx_0008"] is Outcome.ALREADY_RESOLVED and second["ptx_0020"] is Outcome.ALREADY_RESOLVED


def test_the_report_balances_and_only_the_amount_mismatch_is_left_for_a_person(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    flow.services.resolve.execute(DAY)
    report = flow.services.report.execute(DAY)
    assert report.balances and [line.provider_txn_id for line in report.worklist] == ["ptx_0025"]
    assert [b.finding.provider_txn_id for b in flow.services.worklist.execute()] == ["ptx_0025"]


def test_the_carried_timing_difference_clears_on_the_next_day_with_no_action(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    flow.services.resolve.execute(DAY)
    flow.services.simulator.run(DayPlan(NEXT, 10, 44, "ptx_n"))
    outcome = flow.services.reconcile.execute(NEXT)
    assert [b.finding.provider_txn_id for b in outcome.cleared_carried] == ["ptx_0005"] and outcome.breaks == []


def test_reconciling_a_day_twice_updates_the_same_break_rows(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    flow.services.reconcile.execute(DAY)
    with flow.services.uow_factory() as uow:
        assert len(uow.breaks.for_run(DAY)) == 6


def test_eight_workers_resolving_one_break_post_one_correction(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    with flow.services.uow_factory() as uow:
        [fee_break] = [b for b in uow.breaks.for_run(DAY) if b.finding.cause is Cause.FEE_DISCREPANCY]
        rows_before = len(uow.ledger.rows_for(uow.payments.for_txn("ptx_0008")[0].id))
    barrier = threading.Barrier(8)

    def work() -> Outcome:
        barrier.wait(timeout=30)
        outcome: Outcome = flow.services.resolve.resolve(fee_break.id).outcome
        return outcome

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = [future.result(timeout=120) for future in [pool.submit(work) for _ in range(8)]]
    assert sorted(o.value for o in outcomes) == ["already resolved"] * 7 + ["resolved"]
    with flow.services.uow_factory() as uow:
        assert len(uow.ledger.rows_for(uow.payments.for_txn("ptx_0008")[0].id)) == rows_before + 2
    assert digest(flow)[1] == "0.00"


def test_a_resolver_that_fails_midway_leaves_neither_the_postings_nor_the_status(flow, monkeypatch):
    from payments_reconciliation.infrastructure.postgres import PostgresPayments

    planted(flow)
    flow.services.reconcile.execute(DAY)
    with flow.services.uow_factory() as uow:
        [fee_break] = [b for b in uow.breaks.for_run(DAY) if b.finding.cause is Cause.FEE_DISCREPANCY]
    before = digest(flow)

    def explode(self, payment):
        raise RuntimeError("connection reset")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresPayments, "save", explode)
        with pytest.raises(RuntimeError):
            flow.services.resolve.resolve(fee_break.id)
    assert digest(flow) == before
    with flow.services.uow_factory() as uow:
        stored = uow.breaks.get_for_update(fee_break.id)
        assert stored is not None and stored.status is BreakStatus.OPEN
    assert flow.services.resolve.resolve(fee_break.id).outcome is Outcome.RESOLVED


def test_a_process_that_dies_after_the_provider_captured_leaves_no_row_and_the_provider_has_it(flow):
    result = flow.services.simulator.run(DayPlan(DAY, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    assert result.crashed_after_provider == ["ptx_0012"]
    with flow.services.uow_factory() as uow:
        assert uow.payments.for_txn("ptx_0012") == []
    assert flow.services.provider.executed["capture"] == 60


def test_the_health_probe_reports_the_database(flow):
    assert flow.services.health.execute() == {"database": "ok"}
