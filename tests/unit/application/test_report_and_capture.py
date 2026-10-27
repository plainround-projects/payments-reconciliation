from datetime import UTC, date, datetime

import pytest

from payments_reconciliation.application.daily_report import ReportNotAvailableError, render
from payments_reconciliation.application.ports import CaptureResult
from payments_reconciliation.application.record_capture import RecordCapture
from payments_reconciliation.domain.errors import SettlementMissingError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants, SimulatedCrashError
from payments_reconciliation.infrastructure.stub_provider import StubProvider
from tests.unit.fakes import FixedClock, SequentialIds

DAY = date(2026, 10, 20)
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)


def test_reconciling_a_day_without_a_file_names_the_missing_file(world):
    with pytest.raises(SettlementMissingError, match="2026-10-20"):
        world.services.reconcile.execute(DAY)


def test_the_report_of_a_day_that_was_not_reconciled_is_not_available(world):
    world.services.simulator.run(DayPlan(DAY, 10, 1, "ptx_"))
    with pytest.raises(ReportNotAvailableError):
        world.services.report.execute(DAY)


def test_the_report_text_has_the_lessons_sections(tmp_path):
    from tests.unit.conftest import build_world

    world = build_world(tmp_path)
    world.services.simulator.run(DayPlan(DAY, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    world.services.reconcile.execute(DAY)
    world.services.resolve.execute(DAY)
    text = render(world.services.report.execute(DAY))
    assert "Matched at the first pass: 54 transactions" in text
    assert "Auto-resolved: ptx_0008 (fee_discrepancy)" in text
    assert "Resolved from the report: ptx_0012 (missing_capture), ptx_0015 (duplicate_on_our_side)" in text
    assert "ptx_0020 (provider_only_refund)" in text
    assert "Carried to the next day: ptx_0005 (timing)" in text
    assert "Worklist (needs a human): ptx_0025 (amount_mismatch:" in text
    assert "[balances]" in text and "Ledger sum across every posting: 0.00 USD" in text


def test_a_capture_is_recorded_once_per_request_key():
    from tests.unit.fakes import InMemoryDatabase

    db = InMemoryDatabase()
    provider = StubProvider(FeePolicy(290))
    clock = FixedClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    capture = RecordCapture(db.unit_of_work, provider, clock, SequentialIds())
    first = capture.execute("cap-1", Money(4250, "USD"))
    second = capture.execute("cap-1", Money(4250, "USD"))
    assert first == second and len(db.payments) == 1 and len(db.postings) == 6
    assert provider.executed["capture"] == 1 and first.provider_txn_id == "ptx_0001" and first.fee == Money(123, "USD")


def test_a_crash_after_the_provider_captured_leaves_money_moved_and_nothing_recorded():
    from tests.unit.fakes import InMemoryDatabase

    def die(result: CaptureResult) -> None:
        raise SimulatedCrashError("killed")

    db = InMemoryDatabase()
    provider = StubProvider(FeePolicy(290))
    capture = RecordCapture(db.unit_of_work, provider, FixedClock(), SequentialIds(), die)
    with pytest.raises(SimulatedCrashError):
        capture.execute("cap-1", Money(4250, "USD"))
    assert db.payments == {} and db.postings == [] and db.keys == {}
    assert provider.executed["capture"] == 1
