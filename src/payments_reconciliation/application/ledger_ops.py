"""Ledger operations shared by the capture path and the resolvers: each posts whole, balanced entries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from payments_reconciliation.application.ports import UnitOfWork
from payments_reconciliation.domain.ledger import capture_entries
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState


def record_known_capture(
    uow: UnitOfWork,
    *,
    payment_id: UUID,
    provider_txn_id: str,
    amount: Money,
    fee: Money,
    captured_at: datetime,
    source: PaymentSource,
    idempotency_key: str | None = None,
) -> LedgerPayment:
    """Insert a captured payment and its capture entries (six rows) in the caller's transaction."""
    payment = LedgerPayment(
        id=payment_id,
        provider_txn_id=provider_txn_id,
        amount=amount,
        fee=fee,
        state=PaymentState.CAPTURED,
        source=source,
        captured_at=captured_at,
        refunded=Money.zero(amount.currency),
    )
    uow.payments.add(payment, idempotency_key)
    uow.ledger.post(payment_id, "CAPTURE", "capture", capture_entries(amount, fee), captured_at)
    return payment
