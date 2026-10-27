"""The lesson's steps 1 to 6, on the in-memory ledger: the same code path as the real run, without a database."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from payments_reconciliation.application.daily_report import render
from payments_reconciliation.application.ports import BreakStatus
from payments_reconciliation.application.resolve_breaks import Outcome
from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import PaymentSource, PaymentState
from payments_reconciliation.domain.reconciliation import Cause
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants
from tests.unit.conftest import World, build_world

# The lesson plants its six breaks at positions 50, 112, 200, 215, 300 and 400 of 500; most tests here use the same
# six causes at positions a 60-payment day reaches, and one test runs the lesson's exact day.
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)
PLANTED = date(2026, 10, 20)
NEXT = date(2026, 10, 21)
LESSON_SIX = {
    "ptx_0050": Cause.TIMING,
    "ptx_0112": Cause.FEE_DISCREPANCY,
    "ptx_0200": Cause.MISSING_CAPTURE,
    "ptx_0215": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0300": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0400": Cause.AMOUNT_MISMATCH,
}
SIX = {
    "ptx_0005": Cause.TIMING,
    "ptx_0008": Cause.FEE_DISCREPANCY,
    "ptx_0012": Cause.MISSING_CAPTURE,
    "ptx_0015": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0020": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0025": Cause.AMOUNT_MISMATCH,
}


@pytest.fixture(scope="module")
def planted_files(tmp_path_factory):
    """The planted day is simulated once; each test copies the resulting state into its own world."""
    return tmp_path_factory.mktemp("planted")


def planted_world(directory) -> World:
    world = build_world(directory)
    world.services.simulator.run(DayPlan(PLANTED, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    return world


def test_a_clean_day_reconciles_with_zero_breaks_and_every_transaction_matched_on_its_id(world):
    result = world.services.simulator.run(DayPlan(date(2026, 10, 18), 200, 42, "ptx_a"))
    outcome = world.services.reconcile.execute(date(2026, 10, 18))
    assert result.recorded == 200 and outcome.breaks == []
    assert outcome.run.clean_transactions == 200 and outcome.run.break_count == 0
    assert world.db.ledger_sum() == 0


def test_the_same_seed_produces_the_same_day(tmp_path):
    first, second = build_world(tmp_path / "a"), build_world(tmp_path / "b")
    for world in (first, second):
        world.services.simulator.run(DayPlan(PLANTED, 120, 7, "ptx_", plant_breaks=False))
    assert first.services.files.read(PLANTED) == second.services.files.read(PLANTED)


def test_the_planted_day_finds_all_six_breaks_each_with_its_own_cause(tmp_path):
    world = planted_world(tmp_path)
    outcome = world.services.reconcile.execute(PLANTED)
    found = {stored.finding.provider_txn_id: stored.finding.cause for stored in outcome.breaks}
    assert found == SIX
    assert outcome.run.break_count == 6 and outcome.run.clean_transactions == 54


def test_the_lessons_exact_day_of_500_payments_has_the_six_breaks_at_the_lessons_ids(tmp_path):
    world = build_world(tmp_path)
    world.services.simulator.run(DayPlan(PLANTED, 500, 43, "ptx_", plant_breaks=True))
    outcome = world.services.reconcile.execute(PLANTED)
    assert {b.finding.provider_txn_id: b.finding.cause for b in outcome.breaks} == LESSON_SIX
    assert outcome.run.clean_transactions == 494 and outcome.run.settlement_rows == 500


def test_the_breaks_are_planted_by_cause_not_by_editing_files(tmp_path):
    world = build_world(tmp_path)
    result = world.services.simulator.run(DayPlan(PLANTED, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    assert result.crashed_after_provider == ["ptx_0012"]
    assert result.duplicated == ["ptx_0015"]
    assert result.refunded_at_provider_only == ["ptx_0020"]
    assert world.db.payments_of("ptx_0012") == [] and len(world.db.payments_of("ptx_0015")) == 2
    assert world.services.provider.executed["capture"] == 60  # the crashed one was captured at the provider


def test_resolving_fixes_the_four_breaks_that_can_be_fixed_and_leaves_the_amount_for_a_person(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    resolutions = {item.stored.finding.provider_txn_id: item for item in world.services.resolve.execute(PLANTED)}
    assert {k: v.outcome for k, v in resolutions.items()} == {
        "ptx_0005": Outcome.CARRIED,
        "ptx_0008": Outcome.RESOLVED,
        "ptx_0012": Outcome.RESOLVED,
        "ptx_0015": Outcome.RESOLVED,
        "ptx_0020": Outcome.RESOLVED,
        "ptx_0025": Outcome.NEEDS_REVIEW,
    }
    assert resolutions["ptx_0008"].message == "fee corrected, +0.18 posted, ledger balanced"
    status = {b.finding.provider_txn_id: b.status for b in world.db.breaks.values()}
    assert status == {
        "ptx_0005": BreakStatus.CARRIED,
        "ptx_0008": BreakStatus.AUTO_RESOLVED,
        "ptx_0012": BreakStatus.RESOLVED,
        "ptx_0015": BreakStatus.RESOLVED,
        "ptx_0020": BreakStatus.RESOLVED,
        "ptx_0025": BreakStatus.NEEDS_REVIEW,
    }
    assert world.db.ledger_sum() == 0


def test_the_fee_is_corrected_with_one_balanced_entry_and_the_recorded_fee_follows_the_provider(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    payment = world.db.payments_of("ptx_0008")[0]
    assert payment.fee == Money(141, "USD")
    cash = -sum(amount for account, amount in world.db.postings_of(payment.id) if account is Account.MERCHANT_CASH)
    assert cash == 4250 - 141  # what the provider really paid out
    corrections = [row for row in world.db.postings if row[1] == "FEE_CORRECTION"]
    assert [(row[4], row[5].minor) for row in corrections] == [
        (Account.MERCHANT_CASH, 18),
        (Account.PROVIDER_FEES_EXPENSE, -18),
    ]


def test_a_missing_capture_is_inserted_from_the_report_and_the_provider_is_never_called_again(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    captured_at_provider = world.services.provider.executed["capture"]
    world.services.resolve.execute(PLANTED)
    [inserted] = world.db.payments_of("ptx_0012")
    assert (inserted.source, inserted.amount) == (PaymentSource.RECONCILIATION, Money(6000, "USD"))
    assert len(world.db.postings_of(inserted.id)) == 6
    assert world.services.provider.executed["capture"] == captured_at_provider  # money moved once, at the provider
    assert world.services.provider.executed["refund"] == 1


def test_a_duplicate_is_reversed_not_deleted_and_exactly_one_capture_stays(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    first, second = world.db.payments_of("ptx_0015")
    assert (first.state, second.state) == (PaymentState.CAPTURED, PaymentState.DUPLICATE_REVERSED)
    assert len(world.db.postings_of(second.id)) == 12  # the capture's six rows and their six reversals stay on record
    assert sum(amount for _, amount in world.db.postings_of(second.id)) == 0


def test_a_refund_only_the_provider_had_is_posted_from_the_report(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    [payment] = world.db.payments_of("ptx_0020")
    assert payment.refunded == Money(2500, "USD") and len(world.db.postings_of(payment.id)) == 10
    assert world.db.refunds[0].reference == "settlement-refund:ptx_0020"


def test_running_every_resolver_again_changes_nothing(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    with world.db.unit_of_work() as uow:
        before = (uow.ledger.digest(), len(world.db.postings), len(world.db.payments), len(world.db.refunds))
    second = world.services.resolve.execute(PLANTED)
    with world.db.unit_of_work() as uow:
        after = (uow.ledger.digest(), len(world.db.postings), len(world.db.payments), len(world.db.refunds))
    assert before == after
    outcomes = {item.stored.finding.provider_txn_id: item.outcome for item in second}
    assert (
        outcomes["ptx_0008"]
        is outcomes["ptx_0012"]
        is outcomes["ptx_0015"]
        is outcomes["ptx_0020"]
        is Outcome.ALREADY_RESOLVED
    )


def test_resolvers_are_idempotent_even_without_the_break_record(tmp_path):
    """The ledger itself says "already resolved": reopen the break rows and run again."""
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    for stored in list(world.db.breaks.values()):
        if stored.status in {BreakStatus.RESOLVED, BreakStatus.AUTO_RESOLVED}:
            world.db.breaks[stored.id] = dataclasses.replace(stored, status=BreakStatus.OPEN)
    before = len(world.db.postings)
    outcomes = [item.outcome for item in world.services.resolve.execute(PLANTED)]
    assert Outcome.RESOLVED not in outcomes and len(world.db.postings) == before


def test_reconciling_a_day_again_after_the_fixes_leaves_only_the_real_break(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    again = world.services.reconcile.execute(PLANTED)
    assert {b.finding.provider_txn_id for b in again.breaks if b.status is BreakStatus.NEEDS_REVIEW} == {"ptx_0025"}
    assert len(world.db.breaks) == 6  # the same rows, not new ones
    assert [b.finding.provider_txn_id for b in world.services.worklist.execute()] == ["ptx_0025"]


def test_the_daily_report_balances_the_settled_total_and_the_worklist_holds_only_the_amount_mismatch(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    report = world.services.report.execute(PLANTED)
    assert report.balances and report.settled_rows == 59
    assert [line.provider_txn_id for line in report.worklist] == ["ptx_0025"]
    assert [line.provider_txn_id for line in report.auto_resolved] == ["ptx_0008"]
    assert sorted(line.provider_txn_id for line in report.resolved) == ["ptx_0012", "ptx_0015", "ptx_0020"]
    assert [line.provider_txn_id for line in report.carried] == ["ptx_0005"]
    assert report.buckets["worklist"] == Money(9999, "USD")
    assert report.ledger_difference == Money(499, "USD")  # 99.99 at the provider against 95.00 in the ledger
    assert report.ledger_sum == Money(0, "USD") and report.clean_transactions == 54


def test_the_timing_difference_clears_itself_on_the_day_it_settles(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    world.services.simulator.run(DayPlan(NEXT, 30, 44, "ptx_n"))
    outcome = world.services.reconcile.execute(NEXT)
    assert [stored.finding.provider_txn_id for stored in outcome.cleared_carried] == ["ptx_0005"]
    assert outcome.breaks == []
    [timing] = world.db.break_of("ptx_0005")
    assert timing.status is BreakStatus.CLEARED and timing.cleared_on == NEXT
    report = world.services.report.execute(NEXT)
    assert [line.provider_txn_id for line in report.cleared_carried] == ["ptx_0005"] and report.balances


def test_rerunning_the_planted_day_after_its_timing_difference_settled_stays_quiet(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    world.services.simulator.run(DayPlan(NEXT, 30, 44, "ptx_n"))
    world.services.reconcile.execute(NEXT)
    again = world.services.reconcile.execute(PLANTED)
    assert again.run.break_count == 1 and again.run.clean_transactions == 58
    assert [b.finding.provider_txn_id for b in again.breaks if b.status is BreakStatus.NEEDS_REVIEW] == ["ptx_0025"]
    text = render(world.services.report.execute(PLANTED))
    assert "Carried and settled later: ptx_0005" in text


def test_a_timing_difference_that_never_settles_escalates_to_the_worklist(tmp_path):
    world = build_world(tmp_path, max_carry_days=1)
    world.services.simulator.run(
        DayPlan(PLANTED, 60, 5, "ptx_", plant_breaks=True, plants=PLANTS)
    )  # ptx_0005 is late; no ptx_0008+
    world.services.reconcile.execute(PLANTED)
    provider = world.services.provider
    provider._captures.pop("ptx_0005")  # the provider loses it: it will never appear in a report
    for day, prefix in ((NEXT, "ptx_n"), (date(2026, 10, 22), "ptx_m")):
        world.services.simulator.run(DayPlan(day, 5, 6, prefix))
        world.services.reconcile.execute(day)
    causes = {b.finding.cause for b in world.db.break_of("ptx_0005")}
    assert Cause.TIMING_OVERDUE in causes
    overdue = next(b for b in world.db.break_of("ptx_0005") if b.finding.cause is Cause.TIMING_OVERDUE)
    assert overdue.status is BreakStatus.NEEDS_REVIEW
    assert any(b.status is BreakStatus.ESCALATED for b in world.db.break_of("ptx_0005"))


def test_a_break_that_comes_back_after_it_was_resolved_is_reopened(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    payment = world.db.payments_of("ptx_0008")[0]
    world.db.payments[payment.id] = payment.with_fee(Money(123, "USD"))  # someone put the old fee back
    world.services.reconcile.execute(PLANTED)
    [fee_break] = world.db.break_of("ptx_0008", "fee_discrepancy")
    assert fee_break.status is BreakStatus.OPEN and fee_break.note == "reappeared after it was resolved"


def test_a_break_that_no_longer_reproduces_is_cleared(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    payment = world.db.payments_of("ptx_0008")[0]
    world.db.payments[payment.id] = payment.with_fee(Money(141, "USD"))  # fixed by hand, outside the job
    world.services.reconcile.execute(PLANTED)
    [fee_break] = world.db.break_of("ptx_0008", "fee_discrepancy")
    assert fee_break.status is BreakStatus.CLEARED


def test_a_refund_cannot_be_posted_for_a_payment_the_ledger_does_not_have(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    [refund_break] = world.db.break_of("ptx_0020", "provider_only_refund")
    payment = world.db.payments_of("ptx_0020")[0]
    del world.db.payments[payment.id]
    resolution = world.services.resolve.resolve(refund_break.id)
    assert resolution.outcome is Outcome.NEEDS_REVIEW and world.db.refunds == []


def test_an_unknown_break_id_is_an_error(world):
    with pytest.raises(LookupError):
        world.services.resolve.resolve(999)
