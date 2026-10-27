"""The double-entry ledger: entries that cannot be unbalanced, and the entries of each operation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from payments_reconciliation.domain.accounts import Account, Side
from payments_reconciliation.domain.errors import UnbalancedEntryError
from payments_reconciliation.domain.money import Money


class Leg(StrEnum):
    """Which part of an operation an entry books. The pair (payment, operation, reference, leg) is unique."""

    CAPTURE_GROSS = "CAPTURE_GROSS"
    CAPTURE_NET = "CAPTURE_NET"
    CAPTURE_FEE = "CAPTURE_FEE"
    REFUND_GROSS = "REFUND_GROSS"
    REFUND_NET = "REFUND_NET"
    FEE_CORRECTION = "FEE_CORRECTION"
    REVERSAL_GROSS = "REVERSAL_GROSS"
    REVERSAL_NET = "REVERSAL_NET"
    REVERSAL_FEE = "REVERSAL_FEE"


_REVERSAL_OF = {
    Leg.CAPTURE_GROSS: Leg.REVERSAL_GROSS,
    Leg.CAPTURE_NET: Leg.REVERSAL_NET,
    Leg.CAPTURE_FEE: Leg.REVERSAL_FEE,
}


@dataclass(frozen=True, slots=True)
class Posting:
    """One row of the ledger: an account and a signed amount (positive debit, negative credit)."""

    account: Account
    amount: Money


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A debit and an equal credit on two different accounts. Any other shape cannot be built."""

    leg: Leg
    debit: Account
    credit: Account
    amount: Money

    def __post_init__(self) -> None:
        if not self.amount.is_positive():
            raise UnbalancedEntryError(f"an entry moves a positive amount, got {self.amount.format()}")
        if self.debit is self.credit:
            raise UnbalancedEntryError("the debit and the credit must be on different accounts")

    def postings(self) -> tuple[Posting, Posting]:
        """The two rows: +amount on the debit account, -amount on the credit account."""
        return Posting(self.debit, self.amount), Posting(self.credit, -self.amount)

    def reversed(self) -> JournalEntry:
        """The same amount moved back: debit and credit swapped, on the matching reversal leg."""
        return JournalEntry(_REVERSAL_OF[self.leg], self.credit, self.debit, self.amount)


def capture_entries(amount: Money, fee: Money) -> tuple[JournalEntry, ...]:
    """The entries of a capture: gross to pending, net to cash, fee to the fee account (six rows).

    A leg that would move nothing is left out, so a fee that rounds to zero gives four rows.
    """
    if fee.minor < 0 or fee > amount:
        raise UnbalancedEntryError("the fee must be between zero and the captured amount")
    net = amount - fee
    entries = [JournalEntry(Leg.CAPTURE_GROSS, Account.CUSTOMER_RECEIVABLE, Account.MERCHANT_PENDING, amount)]
    if net.is_positive():
        entries.append(JournalEntry(Leg.CAPTURE_NET, Account.MERCHANT_PENDING, Account.MERCHANT_CASH, net))
    if fee.is_positive():
        entries.append(JournalEntry(Leg.CAPTURE_FEE, Account.MERCHANT_PENDING, Account.PROVIDER_FEES_EXPENSE, fee))
    return tuple(entries)


def reversal_entries(amount: Money, fee: Money) -> tuple[JournalEntry, ...]:
    """The entries that undo a capture of `amount` with `fee`, leg by leg. Nothing is deleted; the ledger shows both."""
    return tuple(entry.reversed() for entry in capture_entries(amount, fee))


def refund_entries(amount: Money) -> tuple[JournalEntry, ...]:
    """The two entries (four rows) of a refund: the capture's first two legs reversed. The fee is not returned."""
    return (
        JournalEntry(Leg.REFUND_GROSS, Account.MERCHANT_PENDING, Account.CUSTOMER_RECEIVABLE, amount),
        JournalEntry(Leg.REFUND_NET, Account.MERCHANT_CASH, Account.MERCHANT_PENDING, amount),
    )


def fee_correction_entry(recorded: Money, actual: Money) -> JournalEntry | None:
    """The one entry that moves a recorded fee to the provider's actual fee, or None when they already agree.

    A higher actual fee takes the difference out of the merchant's cash and into the fee account; a lower one
    puts it back.
    """
    delta = actual - recorded
    if delta.is_zero():
        return None
    if delta.is_positive():
        return JournalEntry(Leg.FEE_CORRECTION, Account.MERCHANT_CASH, Account.PROVIDER_FEES_EXPENSE, delta)
    return JournalEntry(Leg.FEE_CORRECTION, Account.PROVIDER_FEES_EXPENSE, Account.MERCHANT_CASH, -delta)


def entries_postings(entries: Iterable[JournalEntry]) -> list[Posting]:
    """Every row of `entries`, in order."""
    return [posting for entry in entries for posting in entry.postings()]


def ledger_sum(postings: Iterable[Posting], currency: str) -> Money:
    """The sum of every posting. It is zero for any set of whole entries."""
    total = Money.zero(currency)
    for posting in postings:
        total = total + posting.amount
    return total


def balances(postings: Iterable[Posting], currency: str) -> dict[Account, Money]:
    """Each account's balance on its normal side (a credit-normal account reads positive after credits)."""
    raw = {account: Money.zero(currency) for account in Account}
    for posting in postings:
        raw[posting.account] = raw[posting.account] + posting.amount
    return {account: (total if account.normal_side is Side.DEBIT else -total) for account, total in raw.items()}
