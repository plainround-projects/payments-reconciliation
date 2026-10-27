"""Matching on the stable id and classifying each difference by its own cause."""

from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest

from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState
from payments_reconciliation.domain.reconciliation import Cause, Handling, LedgerView, cutoff_instant, reconcile
from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, SettlementRow

DAY = date(2026, 10, 20)
CUTOFF = time(23, 0)
USD = "USD"
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LATE = datetime(2026, 10, 20, 23, 40, tzinfo=UTC)
STAMP = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)


def payment(txn, amount=4250, fee=123, at=NOON, state=PaymentState.CAPTURED, serial=1):
    return LedgerPayment(
        UUID(int=serial), txn, Money(amount, USD), Money(fee, USD), state, PaymentSource.SERVICE, at, Money(0, USD)
    )


def settled(txn, amount=4250, fee=123):
    return SettlementRow(txn, SettlementKind.SETTLED, Money(amount, USD), Money(fee, USD), STAMP)


def refunded(txn, amount):
    return SettlementRow(txn, SettlementKind.REFUNDED, Money(-amount, USD), Money(0, USD), STAMP)


def run(payments, rows, refunds=None, carried=None, max_carry_days=2):
    report = SettlementReport(DAY, USD, tuple(rows))
    ledger = LedgerView(tuple(payments), refunds or {})
    return reconcile(ledger, report, carried_days=carried or {}, cutoff=CUTOFF, max_carry_days=max_carry_days)


def causes(result):
    return {item.provider_txn_id: item.cause for item in result.breaks}


def test_a_clean_day_has_no_breaks_and_every_transaction_matched():
    result = run([payment("ptx_0001"), payment("ptx_0002", serial=2)], [settled("ptx_0001"), settled("ptx_0002")])
    assert result.breaks == () and [m.provider_txn_id for m in result.matched] == ["ptx_0001", "ptx_0002"]


def test_the_join_is_on_the_provider_id_not_on_amount_and_time():
    # Same amount, same minute, different ids: a fuzzy join would pair them; the stable id does not.
    result = run([payment("ptx_0001")], [settled("ptx_0002")])
    assert causes(result) == {"ptx_0001": Cause.LEDGER_ONLY_UNEXPLAINED, "ptx_0002": Cause.MISSING_CAPTURE}


def test_the_order_of_the_rows_does_not_matter():
    rows = [settled("ptx_0003"), settled("ptx_0001"), settled("ptx_0002")]
    payments = [payment("ptx_0002", serial=2), payment("ptx_0003", serial=3), payment("ptx_0001")]
    assert run(payments, rows).breaks == ()


def test_a_capture_after_the_cutoff_that_is_not_in_the_report_is_a_timing_difference():
    result = run([payment("ptx_0050", at=LATE)], [])
    assert causes(result) == {"ptx_0050": Cause.TIMING}
    assert result.breaks[0].handling is Handling.CARRY


def test_a_capture_before_the_cutoff_that_is_not_in_the_report_is_not_explained():
    result = run([payment("ptx_0051")], [])
    assert causes(result) == {"ptx_0051": Cause.LEDGER_ONLY_UNEXPLAINED}
    assert result.breaks[0].handling is Handling.HUMAN


def test_the_fee_difference_is_classified_apart_from_the_amount_difference():
    result = run([payment("ptx_0112")], [settled("ptx_0112", fee=141)])
    assert causes(result) == {"ptx_0112": Cause.FEE_DISCREPANCY}
    assert result.breaks[0].detail == "fee recorded 1.23, settled 1.41"


def test_a_different_amount_is_a_real_break_even_when_the_fee_differs_too():
    result = run([payment("ptx_0400", amount=9500)], [settled("ptx_0400", amount=9999, fee=290)])
    assert causes(result) == {"ptx_0400": Cause.AMOUNT_MISMATCH}
    assert result.breaks[0].handling is Handling.HUMAN


def test_settled_at_the_provider_with_nothing_in_the_ledger_is_a_missing_capture():
    result = run([], [settled("ptx_0200", amount=6000, fee=174)])
    assert causes(result) == {"ptx_0200": Cause.MISSING_CAPTURE}
    assert (result.breaks[0].settlement_amount, result.breaks[0].settlement_fee) == (Money(6000, USD), Money(174, USD))


def test_two_active_payments_for_one_provider_id_are_a_duplicate_on_our_side():
    result = run([payment("ptx_0215", serial=1), payment("ptx_0215", serial=2)], [settled("ptx_0215")])
    assert causes(result) == {"ptx_0215": Cause.DUPLICATE_ON_OUR_SIDE}


def test_a_reversed_duplicate_no_longer_counts():
    payments = [payment("ptx_0215", serial=1), payment("ptx_0215", serial=2, state=PaymentState.DUPLICATE_REVERSED)]
    assert run(payments, [settled("ptx_0215")]).breaks == ()


def test_a_refund_only_the_provider_has_is_a_provider_only_refund():
    result = run([payment("ptx_0300")], [settled("ptx_0300"), refunded("ptx_0300", 4250)])
    assert causes(result) == {"ptx_0300": Cause.PROVIDER_ONLY_REFUND}
    assert result.breaks[0].kind is SettlementKind.REFUNDED and result.breaks[0].settlement_amount == Money(4250, USD)
    assert [m.kind for m in result.matched] == [SettlementKind.SETTLED]


def test_a_refund_in_both_that_agrees_matches():
    result = run(
        [payment("ptx_0300")], [settled("ptx_0300"), refunded("ptx_0300", 1000)], refunds={"ptx_0300": Money(1000, USD)}
    )
    assert result.breaks == ()


def test_a_refund_in_the_ledger_only_or_for_another_amount_needs_a_person():
    only_ledger = run([payment("ptx_0300")], [settled("ptx_0300")], refunds={"ptx_0300": Money(1000, USD)})
    assert causes(only_ledger) == {"ptx_0300": Cause.REFUND_ONLY_IN_LEDGER}
    different = run(
        [payment("ptx_0300")], [settled("ptx_0300"), refunded("ptx_0300", 900)], refunds={"ptx_0300": Money(1000, USD)}
    )
    assert causes(different) == {"ptx_0300": Cause.REFUND_AMOUNT_MISMATCH}
    assert different.breaks[0].handling is Handling.HUMAN


def test_two_settled_rows_for_one_id_is_a_provider_duplicate():
    result = run([payment("ptx_0001")], [settled("ptx_0001"), settled("ptx_0001")])
    assert causes(result) == {"ptx_0001": Cause.PROVIDER_DUPLICATE}


def test_a_carried_difference_that_is_still_not_in_the_report_keeps_waiting():
    result = run([payment("ptx_0050", at=datetime(2026, 10, 19, 23, 40, tzinfo=UTC))], [], carried={"ptx_0050": 1})
    assert causes(result) == {"ptx_0050": Cause.TIMING}


def test_a_carried_difference_that_waited_too_long_goes_to_a_person():
    result = run([payment("ptx_0050", at=datetime(2026, 10, 18, 23, 40, tzinfo=UTC))], [], carried={"ptx_0050": 2})
    assert causes(result) == {"ptx_0050": Cause.TIMING_OVERDUE}
    assert result.breaks[0].handling is Handling.HUMAN


def test_a_carried_difference_that_arrives_matches_with_no_break():
    result = run(
        [payment("ptx_0050", at=datetime(2026, 10, 19, 23, 40, tzinfo=UTC))],
        [settled("ptx_0050")],
        carried={"ptx_0050": 1},
    )
    assert result.breaks == () and [m.provider_txn_id for m in result.matched] == ["ptx_0050"]


def test_every_cause_has_a_handling():
    from payments_reconciliation.domain.reconciliation import HANDLING

    assert set(HANDLING) == set(Cause)


@pytest.mark.parametrize(
    ("cause", "handling"),
    [
        (Cause.TIMING, Handling.CARRY),
        (Cause.FEE_DISCREPANCY, Handling.AUTOMATIC),
        (Cause.MISSING_CAPTURE, Handling.FROM_REPORT),
        (Cause.DUPLICATE_ON_OUR_SIDE, Handling.FROM_REPORT),
        (Cause.PROVIDER_ONLY_REFUND, Handling.FROM_REPORT),
        (Cause.AMOUNT_MISMATCH, Handling.HUMAN),
    ],
)
def test_the_lessons_six_causes_are_handled_as_the_lesson_says(cause, handling):
    from payments_reconciliation.domain.reconciliation import HANDLING

    assert HANDLING[cause] is handling


def test_the_cutoff_instant_is_the_batch_close_in_utc():
    assert cutoff_instant(DAY, CUTOFF) == datetime(2026, 10, 20, 23, 0, tzinfo=UTC)


def test_a_payment_can_carry_a_refund_up_to_its_amount_only():
    from payments_reconciliation.domain.errors import ReconciliationError

    base = payment("ptx_0001")
    assert base.with_refund(Money(1000, USD)).refunded == Money(1000, USD)
    with pytest.raises(ReconciliationError):
        base.with_refund(Money(4251, USD))
    with pytest.raises(ReconciliationError):
        base.with_refund(Money(0, USD))
    assert base.reversed_as_duplicate().active is False and base.with_fee(Money(141, USD)).fee == Money(141, USD)
