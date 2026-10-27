import pytest

from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.errors import UnbalancedEntryError
from payments_reconciliation.domain.ledger import (
    JournalEntry,
    Leg,
    balances,
    capture_entries,
    entries_postings,
    fee_correction_entry,
    ledger_sum,
    refund_entries,
    reversal_entries,
)
from payments_reconciliation.domain.money import Money

USD = "USD"


def test_the_worked_example_capture_is_six_rows_that_sum_to_zero():
    postings = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    assert [(p.account.value, p.amount.minor) for p in postings] == [
        ("customer_receivable", 4250),
        ("merchant_pending", -4250),
        ("merchant_pending", 4127),
        ("merchant_cash", -4127),
        ("merchant_pending", 123),
        ("provider_fees_expense", -123),
    ]
    assert ledger_sum(postings, USD) == Money(0, USD)


def test_an_entry_on_one_account_or_of_zero_cannot_be_built():
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(Leg.CAPTURE_NET, Account.MERCHANT_CASH, Account.MERCHANT_CASH, Money(1, USD))
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(Leg.CAPTURE_NET, Account.MERCHANT_PENDING, Account.MERCHANT_CASH, Money(0, USD))


def test_a_fee_outside_zero_to_the_amount_is_refused():
    with pytest.raises(UnbalancedEntryError):
        capture_entries(Money(100, USD), Money(101, USD))
    with pytest.raises(UnbalancedEntryError):
        capture_entries(Money(100, USD), Money(-1, USD))


def test_legs_that_would_move_nothing_are_left_out():
    assert [e.leg for e in capture_entries(Money(1, USD), Money(0, USD))] == [Leg.CAPTURE_GROSS, Leg.CAPTURE_NET]
    assert [e.leg for e in capture_entries(Money(100, USD), Money(100, USD))] == [Leg.CAPTURE_GROSS, Leg.CAPTURE_FEE]


def test_a_reversal_undoes_every_leg_of_the_capture_and_both_stay_in_the_ledger():
    captured = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    reversed_ = entries_postings(reversal_entries(Money(4250, USD), Money(123, USD)))
    assert len(reversed_) == 6 and ledger_sum(reversed_, USD) == Money(0, USD)
    assert all(value == Money(0, USD) for value in balances(captured + reversed_, USD).values())


def test_a_reversal_uses_reversal_legs():
    legs = [entry.leg for entry in reversal_entries(Money(4250, USD), Money(123, USD))]
    assert legs == [Leg.REVERSAL_GROSS, Leg.REVERSAL_NET, Leg.REVERSAL_FEE]


def test_a_higher_actual_fee_takes_the_difference_out_of_cash_into_the_fee_account():
    entry = fee_correction_entry(Money(123, USD), Money(141, USD))
    assert entry is not None and entry.amount == Money(18, USD)
    captured = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    after = balances(captured + list(entry.postings()), USD)
    assert after[Account.MERCHANT_CASH] == Money(4127 - 18, USD)
    assert after[Account.PROVIDER_FEES_EXPENSE] == Money(141, USD)


def test_a_lower_actual_fee_puts_the_difference_back():
    entry = fee_correction_entry(Money(141, USD), Money(123, USD))
    assert entry is not None and (entry.debit, entry.credit) == (Account.PROVIDER_FEES_EXPENSE, Account.MERCHANT_CASH)


def test_equal_fees_need_no_correction():
    assert fee_correction_entry(Money(123, USD), Money(123, USD)) is None


def test_a_refund_returns_money_and_keeps_the_fee():
    captured = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    refunded = entries_postings(refund_entries(Money(1000, USD)))
    result = balances(captured + refunded, USD)
    assert ledger_sum(refunded, USD) == Money(0, USD)
    assert result[Account.MERCHANT_CASH] == Money(3127, USD) and result[Account.PROVIDER_FEES_EXPENSE] == Money(
        123, USD
    )
