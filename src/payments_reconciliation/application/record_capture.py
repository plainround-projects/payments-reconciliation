"""The capture path of the service this project reconciles: capture at the provider, record it in the ledger."""

from __future__ import annotations

import logging
from collections.abc import Callable

from payments_reconciliation.application.ledger_ops import record_known_capture
from payments_reconciliation.application.logs import fields
from payments_reconciliation.application.ports import CaptureResult, Clock, IdSource, PaymentProvider, UnitOfWorkFactory
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource

logger = logging.getLogger(__name__)


class RecordCapture:
    """Capture `amount` at the provider and record the payment and its six ledger rows in one transaction.

    Idempotent on the request key: a repeat finds the payment already recorded and returns it. `after_provider_call`
    runs between the provider's answer and the database write; the simulation uses it to crash there, which leaves
    money moved at the provider and nothing in the ledger.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        provider: PaymentProvider,
        clock: Clock,
        ids: IdSource,
        after_provider_call: Callable[[CaptureResult], None] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._clock = clock
        self._ids = ids
        self._after_provider_call = after_provider_call

    def execute(self, idempotency_key: str, amount: Money) -> LedgerPayment:
        """Return the recorded payment, creating it the first time the key is seen."""
        with self._uow_factory() as uow:
            existing = uow.payments.by_key(idempotency_key)
            if existing is not None:
                return existing
            now = self._clock.now()
            result = self._provider.capture(idempotency_key=idempotency_key, amount=amount, occurred_at=now)
            if self._after_provider_call is not None:
                self._after_provider_call(result)
            payment = record_known_capture(
                uow,
                payment_id=self._ids.new_id(),
                provider_txn_id=result.provider_txn_id,
                amount=amount,
                fee=result.fee,
                captured_at=now,
                source=PaymentSource.SERVICE,
                idempotency_key=idempotency_key,
            )
            logger.info("captured", extra=fields(provider_txn_id=result.provider_txn_id, amount=amount.format()))
            return payment
