"""Matching the ledger against a settlement report and classifying every difference by its cause.

Everything here is a pure function of its inputs: no clock, no database, no files.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum

from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment
from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, SettlementRow


class Cause(StrEnum):
    """Why a transaction does not match. One label per cause, never a shared "mismatch" bucket."""

    TIMING = "timing"
    FEE_DISCREPANCY = "fee_discrepancy"
    MISSING_CAPTURE = "missing_capture"
    DUPLICATE_ON_OUR_SIDE = "duplicate_on_our_side"
    PROVIDER_ONLY_REFUND = "provider_only_refund"
    AMOUNT_MISMATCH = "amount_mismatch"
    # The causes below are the ones no rule can clear safely; they go to a person.
    LEDGER_ONLY_UNEXPLAINED = "ledger_only_unexplained"
    TIMING_OVERDUE = "timing_overdue"
    PROVIDER_DUPLICATE = "provider_duplicate"
    REFUND_ONLY_IN_LEDGER = "refund_only_in_ledger"
    REFUND_AMOUNT_MISMATCH = "refund_amount_mismatch"


class Handling(StrEnum):
    """What is done about a cause."""

    CARRY = "carry"  # wait: the next day's report should contain it
    AUTOMATIC = "automatic"  # a balanced correcting entry from the report's own number
    FROM_REPORT = "from_report"  # fix the ledger from the provider's authoritative data
    HUMAN = "human"  # nothing here can decide


HANDLING: Mapping[Cause, Handling] = {
    Cause.TIMING: Handling.CARRY,
    Cause.FEE_DISCREPANCY: Handling.AUTOMATIC,
    Cause.MISSING_CAPTURE: Handling.FROM_REPORT,
    Cause.DUPLICATE_ON_OUR_SIDE: Handling.FROM_REPORT,
    Cause.PROVIDER_ONLY_REFUND: Handling.FROM_REPORT,
    Cause.AMOUNT_MISMATCH: Handling.HUMAN,
    Cause.LEDGER_ONLY_UNEXPLAINED: Handling.HUMAN,
    Cause.TIMING_OVERDUE: Handling.HUMAN,
    Cause.PROVIDER_DUPLICATE: Handling.HUMAN,
    Cause.REFUND_ONLY_IN_LEDGER: Handling.HUMAN,
    Cause.REFUND_AMOUNT_MISMATCH: Handling.HUMAN,
}


@dataclass(frozen=True, slots=True)
class Break:
    """One difference between the two sides, with the numbers that show it."""

    provider_txn_id: str
    kind: SettlementKind
    cause: Cause
    ledger_amount: Money | None
    settlement_amount: Money | None
    ledger_fee: Money | None
    settlement_fee: Money | None
    detail: str

    @property
    def handling(self) -> Handling:
        """What is done about this break's cause."""
        return HANDLING[self.cause]


@dataclass(frozen=True, slots=True)
class LedgerView:
    """Our side of one business day: the payments to compare and the refunds recorded that day, by provider id."""

    payments: tuple[LedgerPayment, ...]
    refunds: Mapping[str, Money] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Matched:
    """A transaction both sides agree on."""

    provider_txn_id: str
    kind: SettlementKind
    amount: Money


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Everything one pass found."""

    matched: tuple[Matched, ...]
    breaks: tuple[Break, ...]

    def break_ids(self) -> set[str]:
        """Provider ids that have at least one break."""
        return {item.provider_txn_id for item in self.breaks}


def cutoff_instant(business_date: date, cutoff: time) -> datetime:
    """The moment the provider closes the batch of `business_date`; captures at or after it settle the next day."""
    return datetime.combine(business_date, cutoff, tzinfo=UTC)


def reconcile(
    ledger: LedgerView,
    report: SettlementReport,
    *,
    carried_days: Mapping[str, int],
    cutoff: time,
    max_carry_days: int,
) -> MatchResult:
    """Join the two sides on the provider's transaction id and classify what does not agree.

    `carried_days` maps a transaction id to how many days it has waited as a timing difference.
    """
    currency = report.currency
    ours: dict[str, list[LedgerPayment]] = defaultdict(list)
    for payment in ledger.payments:
        if payment.active:
            ours[payment.provider_txn_id].append(payment)
    settled: dict[str, list[SettlementRow]] = defaultdict(list)
    refunded: dict[str, list[SettlementRow]] = defaultdict(list)
    for row in report.rows:
        (settled if row.kind is SettlementKind.SETTLED else refunded)[row.provider_txn_id].append(row)
    matched: list[Matched] = []
    breaks: list[Break] = []
    for txn_id in sorted(set(ours) | set(settled)):
        found = _classify_settled(
            txn_id,
            ours.get(txn_id, []),
            settled.get(txn_id, []),
            report=report,
            carried_days=carried_days.get(txn_id),
            cutoff=cutoff,
            max_carry_days=max_carry_days,
        )
        if isinstance(found, Break):
            breaks.append(found)
        elif found is not None:
            matched.append(found)
    for txn_id in sorted(set(ledger.refunds) | set(refunded)):
        found = _classify_refunds(txn_id, ledger.refunds.get(txn_id), refunded.get(txn_id, []), currency)
        if isinstance(found, Break):
            breaks.append(found)
        elif found is not None:
            matched.append(found)
    return MatchResult(tuple(matched), tuple(breaks))


def _classify_settled(
    txn_id: str,
    ours: list[LedgerPayment],
    theirs: list[SettlementRow],
    *,
    report: SettlementReport,
    carried_days: int | None,
    cutoff: time,
    max_carry_days: int,
) -> Break | Matched | None:
    kind = SettlementKind.SETTLED
    if len(theirs) > 1:
        total = sum((row.amount.minor for row in theirs), 0)
        return Break(
            txn_id, kind, Cause.PROVIDER_DUPLICATE, _total(ours), Money(total, report.currency), None, None,
            f"the report lists {len(theirs)} settled rows for one transaction",
        )  # fmt: skip
    if len(ours) > 1:
        return Break(
            txn_id, kind, Cause.DUPLICATE_ON_OUR_SIDE, ours[0].amount, theirs[0].amount if theirs else None,
            ours[0].fee, theirs[0].fee if theirs else None,
            f"{len(ours)} payments in the ledger carry this provider transaction id",
        )  # fmt: skip
    if not ours:
        row = theirs[0]
        return Break(
            txn_id, kind, Cause.MISSING_CAPTURE, None, row.amount, None, row.fee,
            "settled at the provider, no payment in the ledger",
        )  # fmt: skip
    payment = ours[0]
    if not theirs:
        return _ledger_only(txn_id, payment, report.business_date, carried_days, cutoff, max_carry_days)
    row = theirs[0]
    if payment.amount != row.amount:
        return Break(
            txn_id, kind, Cause.AMOUNT_MISMATCH, payment.amount, row.amount, payment.fee, row.fee,
            f"ledger {payment.amount.format()}, settlement {row.amount.format()}",
        )  # fmt: skip
    if payment.fee != row.fee:
        return Break(
            txn_id, kind, Cause.FEE_DISCREPANCY, payment.amount, row.amount, payment.fee, row.fee,
            f"fee recorded {payment.fee.format()}, settled {row.fee.format()}",
        )  # fmt: skip
    return Matched(txn_id, kind, payment.amount)


def _ledger_only(
    txn_id: str,
    payment: LedgerPayment,
    business_date: date,
    carried_days: int | None,
    cutoff: time,
    max_carry_days: int,
) -> Break:
    base = (txn_id, SettlementKind.SETTLED)
    amounts = (payment.amount, None, payment.fee, None)
    if carried_days is not None:
        if carried_days >= max_carry_days:
            return Break(*base, Cause.TIMING_OVERDUE, *amounts, f"still not settled after {carried_days} days")
        return Break(*base, Cause.TIMING, *amounts, f"waiting for the next report (day {carried_days + 1})")
    if payment.captured_at >= cutoff_instant(business_date, cutoff):
        return Break(*base, Cause.TIMING, *amounts, f"captured after the {cutoff:%H:%M} cutoff; settles the next day")
    return Break(*base, Cause.LEDGER_ONLY_UNEXPLAINED, *amounts, "captured before the cutoff and not in the report")


def _total(payments: list[LedgerPayment]) -> Money | None:
    return payments[0].amount if payments else None


def _classify_refunds(
    txn_id: str, ours: Money | None, theirs: list[SettlementRow], currency: str
) -> Break | Matched | None:
    kind = SettlementKind.REFUNDED
    provider_total = -Money(sum((row.amount.minor for row in theirs), 0), currency) if theirs else None
    ledger_total = ours if ours is not None and ours.is_positive() else None
    if provider_total is not None and ledger_total is None:
        return Break(
            txn_id, kind, Cause.PROVIDER_ONLY_REFUND, None, provider_total, None, None,
            f"the provider refunded {provider_total.format()}, the ledger shows no refund",
        )  # fmt: skip
    if ledger_total is not None and provider_total is None:
        return Break(
            txn_id, kind, Cause.REFUND_ONLY_IN_LEDGER, ledger_total, None, None, None,
            f"the ledger shows a refund of {ledger_total.format()}, the provider's report has none",
        )  # fmt: skip
    if ledger_total is None or provider_total is None:
        return None
    if ledger_total != provider_total:
        return Break(
            txn_id, kind, Cause.REFUND_AMOUNT_MISMATCH, ledger_total, provider_total, None, None,
            f"ledger refunded {ledger_total.format()}, provider refunded {provider_total.format()}",
        )  # fmt: skip
    return Matched(txn_id, kind, ledger_total)
