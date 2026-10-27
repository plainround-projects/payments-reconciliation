"""A captured payment as the ledger knows it, carrying the provider's transaction id."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payments_reconciliation.domain.errors import ReconciliationError
from payments_reconciliation.domain.money import Money


class PaymentState(StrEnum):
    """CAPTURED payments count in reconciliation; a duplicate that was reversed does not."""

    CAPTURED = "CAPTURED"
    DUPLICATE_REVERSED = "DUPLICATE_REVERSED"


class PaymentSource(StrEnum):
    """How the payment got into the ledger."""

    SERVICE = "SERVICE"  # the capture path, as it should work
    SERVICE_RETRY = "SERVICE_RETRY"  # recorded a second time by a retry (the duplicate break)
    RECONCILIATION = "RECONCILIATION"  # inserted from the settlement report (the missing-capture break)


@dataclass(frozen=True, slots=True)
class LedgerPayment:
    """One row of `payments`. Several rows may carry one provider transaction id; that is the duplicate break."""

    id: UUID
    provider_txn_id: str
    amount: Money
    fee: Money
    state: PaymentState
    source: PaymentSource
    captured_at: datetime
    refunded: Money

    @property
    def active(self) -> bool:
        """True while the payment counts as money moved."""
        return self.state is PaymentState.CAPTURED

    def with_fee(self, fee: Money) -> LedgerPayment:
        """The same payment with the provider's actual fee."""
        return dataclasses.replace(self, fee=fee)

    def reversed_as_duplicate(self) -> LedgerPayment:
        """The same payment, no longer counted."""
        return dataclasses.replace(self, state=PaymentState.DUPLICATE_REVERSED)

    def with_refund(self, amount: Money) -> LedgerPayment:
        """The same payment after refunding `amount` more; never more than was captured."""
        total = self.refunded + amount
        if not amount.is_positive() or total > self.amount:
            raise ReconciliationError(f"refund of {amount.format()} does not fit a capture of {self.amount.format()}")
        return dataclasses.replace(self, refunded=total)
