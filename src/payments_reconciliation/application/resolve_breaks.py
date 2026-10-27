"""Resolve the breaks that can be resolved safely, each exactly once, never by calling the provider again."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum

from payments_reconciliation.application.ledger_ops import record_known_capture
from payments_reconciliation.application.logs import fields
from payments_reconciliation.application.ports import (
    BreakStatus,
    Clock,
    RefundRecord,
    StoredBreak,
    UnitOfWork,
    UnitOfWorkFactory,
)
from payments_reconciliation.domain.ledger import fee_correction_entry, refund_entries, reversal_entries
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import PaymentSource
from payments_reconciliation.domain.reconciliation import Cause, Handling

logger = logging.getLogger(__name__)

# Missing captures first, so a refund for the same transaction finds its payment; duplicates before fees.
_ORDER = [
    Cause.MISSING_CAPTURE,
    Cause.DUPLICATE_ON_OUR_SIDE,
    Cause.FEE_DISCREPANCY,
    Cause.PROVIDER_ONLY_REFUND,
]
_NAMESPACE = uuid.UUID("6f1c7d5e-0b1a-4c1e-9a53-5d7b7a1c0001")


class Outcome(StrEnum):
    """What a resolution did."""

    RESOLVED = "resolved"
    ALREADY_RESOLVED = "already resolved"
    CARRIED = "carried to the next report"
    NEEDS_REVIEW = "needs a human"


@dataclass(frozen=True, slots=True)
class Resolution:
    """One break's resolution: what happened, in words the daily report can repeat."""

    stored: StoredBreak
    outcome: Outcome
    message: str


_Resolver = Callable[[UnitOfWork, StoredBreak, datetime], tuple[Outcome, str]]


class ResolveBreaks:
    """Apply the rule for each break of a run. Safe to run twice: the second pass changes nothing."""

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._resolvers: dict[Cause, _Resolver] = {
            Cause.MISSING_CAPTURE: self._insert_missing_capture,
            Cause.DUPLICATE_ON_OUR_SIDE: self._reverse_duplicate,
            Cause.FEE_DISCREPANCY: self._correct_fee,
            Cause.PROVIDER_ONLY_REFUND: self._post_provider_refund,
        }

    def execute(self, run_date: date) -> list[Resolution]:
        """Resolve every break of the run in a safe order; one transaction per break."""
        with self._uow_factory() as uow:
            ids = [stored.id for stored in uow.breaks.for_run(run_date)]
        resolutions = [self.resolve(break_id) for break_id in ids]
        order = {cause: position for position, cause in enumerate(_ORDER)}
        return sorted(
            resolutions,
            key=lambda item: (order.get(item.stored.finding.cause, len(order)), item.stored.finding.provider_txn_id),
        )

    def resolve(self, break_id: int) -> Resolution:
        """Resolve one break in its own transaction, under a lock on the break row."""
        with self._uow_factory() as uow:
            stored = uow.breaks.get_for_update(break_id)
            if stored is None:
                raise LookupError(f"no break {break_id}")
            now = self._clock.now()
            cause = stored.finding.cause
            if stored.status in {
                BreakStatus.RESOLVED,
                BreakStatus.AUTO_RESOLVED,
                BreakStatus.CLEARED,
                BreakStatus.ESCALATED,
            }:
                return Resolution(stored, Outcome.ALREADY_RESOLVED, stored.note)
            if stored.finding.handling is Handling.CARRY:
                note = f"no action: carried to tomorrow's reconciliation ({stored.finding.detail})"
                return Resolution(stored, Outcome.CARRIED, note)
            if stored.finding.handling is Handling.HUMAN:
                words = cause.value.replace("_", " ")
                note = f"flagged: {words} requires investigation, not auto-resolvable ({stored.finding.detail})"
                return Resolution(stored, Outcome.NEEDS_REVIEW, note)
            outcome, message = self._resolvers[cause](uow, stored, now)
            if outcome is Outcome.NEEDS_REVIEW:
                uow.breaks.update(stored.id, BreakStatus.NEEDS_REVIEW, message, now)
            else:
                status = (
                    BreakStatus.AUTO_RESOLVED if stored.finding.handling is Handling.AUTOMATIC else BreakStatus.RESOLVED
                )
                uow.breaks.update(stored.id, status, message, now)
            logger.info(
                "break handled",
                extra=fields(provider_txn_id=stored.finding.provider_txn_id, cause=cause.value, outcome=outcome.value),
            )
            return Resolution(stored, outcome, message)

    def _correct_fee(self, uow: UnitOfWork, stored: StoredBreak, now: datetime) -> tuple[Outcome, str]:
        finding = stored.finding
        uow.payments.lock_txn(finding.provider_txn_id)
        active = [p for p in uow.payments.for_txn(finding.provider_txn_id) if p.active]
        if len(active) != 1 or finding.settlement_fee is None:
            return Outcome.NEEDS_REVIEW, "the fee cannot be corrected while the transaction has no single payment"
        payment = active[0]
        entry = fee_correction_entry(payment.fee, finding.settlement_fee)
        if entry is None:
            return Outcome.ALREADY_RESOLVED, f"fee already {payment.fee.format()}"
        delta = finding.settlement_fee - payment.fee
        uow.ledger.post(payment.id, "FEE_CORRECTION", f"fee-{finding.settlement_fee.minor}", [entry], now)
        uow.payments.save(payment.with_fee(finding.settlement_fee))
        sign = "+" if delta.is_positive() else "-"
        shown = Money(abs(delta.minor), delta.currency).format()
        return Outcome.RESOLVED, f"fee corrected, {sign}{shown} posted, ledger balanced"

    def _insert_missing_capture(self, uow: UnitOfWork, stored: StoredBreak, now: datetime) -> tuple[Outcome, str]:
        finding = stored.finding
        uow.payments.lock_txn(finding.provider_txn_id)
        if uow.payments.for_txn(finding.provider_txn_id):
            return Outcome.ALREADY_RESOLVED, "a payment for this transaction exists"
        assert finding.settlement_amount is not None
        assert finding.settlement_fee is not None
        record_known_capture(
            uow,
            payment_id=uuid.uuid5(_NAMESPACE, f"capture:{finding.provider_txn_id}"),
            provider_txn_id=finding.provider_txn_id,
            amount=finding.settlement_amount,
            fee=finding.settlement_fee,
            captured_at=datetime.combine(stored.run_date, time(0, 0), tzinfo=UTC),
            source=PaymentSource.RECONCILIATION,
        )
        return Outcome.RESOLVED, "inserted from settlement report, postings added, ledger balanced"

    def _reverse_duplicate(self, uow: UnitOfWork, stored: StoredBreak, now: datetime) -> tuple[Outcome, str]:
        finding = stored.finding
        uow.payments.lock_txn(finding.provider_txn_id)
        active = sorted(
            (p for p in uow.payments.for_txn(finding.provider_txn_id) if p.active),
            key=lambda p: (p.captured_at, str(p.id)),
        )
        if len(active) <= 1:
            return Outcome.ALREADY_RESOLVED, "one capture is recorded"
        kept = active[0]
        for extra in active[1:]:
            uow.ledger.post(
                extra.id, "REVERSAL", f"duplicate-of:{kept.id}", reversal_entries(extra.amount, extra.fee), now
            )
            uow.payments.save(extra.reversed_as_duplicate())
        return Outcome.RESOLVED, "duplicate reversed, one capture retained, ledger balanced"

    def _post_provider_refund(self, uow: UnitOfWork, stored: StoredBreak, now: datetime) -> tuple[Outcome, str]:
        finding = stored.finding
        uow.payments.lock_txn(finding.provider_txn_id)
        active = [p for p in uow.payments.for_txn(finding.provider_txn_id) if p.active]
        if len(active) != 1 or finding.settlement_amount is None:
            return Outcome.NEEDS_REVIEW, "the refund cannot be posted while the transaction has no single payment"
        payment = active[0]
        reference = f"settlement-refund:{finding.provider_txn_id}"
        if uow.refunds.exists(payment.id, reference):
            return Outcome.ALREADY_RESOLVED, "the refund is already in the ledger"
        at = datetime.combine(stored.run_date, time(0, 0), tzinfo=UTC)
        uow.payments.save(payment.with_refund(finding.settlement_amount))
        uow.refunds.add(
            RefundRecord(uuid.uuid5(_NAMESPACE, reference), payment.id, finding.settlement_amount, reference, at)
        )
        uow.ledger.post(payment.id, "REFUND", reference, refund_entries(finding.settlement_amount), at)
        return Outcome.RESOLVED, "refund posted from settlement report, ledger balanced"
