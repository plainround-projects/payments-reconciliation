"""The stub payment provider: deterministic, honors idempotency keys, and settles in daily batches.

It is the source of truth for what moved: captures and refunds are recorded here the moment they happen, and the
settlement report of a business day is computed from these records. A scenario can make the provider's settled
figures differ from what the payments service recorded (`adjust_fee`, `adjust_amount`), the way a real provider does
after a surcharge or a dispute adjustment.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from payments_reconciliation.application.ports import CaptureResult, RefundResult
from payments_reconciliation.domain.errors import PaymentNotFoundError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind, SettlementRow


@dataclass(slots=True)
class _Capture:
    txn_id: str
    amount: Money
    fee: Money
    captured_at: datetime
    settled_amount: Money
    settled_fee: Money


@dataclass(slots=True)
class _Refund:
    refund_id: str
    txn_id: str
    amount: Money
    refunded_at: datetime


class StubProvider:
    """A `PaymentProvider` kept in memory. Thread-safe; ids are sequential so runs are repeatable."""

    def __init__(self, fee_policy: FeePolicy, id_prefix: str = "ptx_") -> None:
        self._fee_policy = fee_policy
        self._lock = threading.RLock()
        self._prefix = id_prefix
        self._sequence = 0
        self._refund_sequence = 0
        self._captures: dict[str, _Capture] = {}
        self._refunds: list[_Refund] = []
        self._by_key: dict[tuple[str, str], object] = {}
        self.executed: dict[str, int] = {"capture": 0, "refund": 0}
        """How many times each operation really ran (a repeat of a key does not count)."""

    def start_series(self, id_prefix: str) -> None:
        """Begin a new run of transaction ids: `<prefix>0001`, `<prefix>0002`, ..."""
        with self._lock:
            self._prefix = id_prefix
            self._sequence = 0

    def capture(self, *, idempotency_key: str, amount: Money, occurred_at: datetime) -> CaptureResult:
        """Capture once per key and return the provider's own transaction id and fee."""
        with self._lock:
            cached = self._by_key.get(("capture", idempotency_key))
            if isinstance(cached, CaptureResult):
                return cached
            self._sequence += 1
            txn_id = f"{self._prefix}{self._sequence:04d}"
            fee = self._fee_policy.fee_for(amount)
            self._captures[txn_id] = _Capture(txn_id, amount, fee, occurred_at, amount, fee)
            result = CaptureResult(txn_id, fee)
            self._by_key[("capture", idempotency_key)] = result
            self.executed["capture"] += 1
            return result

    def refund(
        self, *, idempotency_key: str, provider_txn_id: str, amount: Money, occurred_at: datetime
    ) -> RefundResult:
        """Refund once per key, never more than was captured."""
        with self._lock:
            cached = self._by_key.get(("refund", idempotency_key))
            if isinstance(cached, RefundResult):
                return cached
            capture = self._captures.get(provider_txn_id)
            if capture is None:
                raise PaymentNotFoundError(f"the provider has no transaction {provider_txn_id}")
            already = sum((r.amount.minor for r in self._refunds if r.txn_id == provider_txn_id), 0)
            if amount.minor + already > capture.amount.minor:
                raise PaymentNotFoundError(f"refund of {amount.format()} exceeds the capture of {provider_txn_id}")
            self._refund_sequence += 1
            refund_id = f"ref_{self._refund_sequence:04d}"
            self._refunds.append(_Refund(refund_id, provider_txn_id, amount, occurred_at))
            result = RefundResult(refund_id)
            self._by_key[("refund", idempotency_key)] = result
            self.executed["refund"] += 1
            return result

    def adjust_fee(self, provider_txn_id: str, fee: Money) -> None:
        """The provider settles with a different fee than it quoted at capture (a surcharge applied later)."""
        with self._lock:
            self._captures[provider_txn_id].settled_fee = fee

    def adjust_amount(self, provider_txn_id: str, amount: Money) -> None:
        """The provider settles a different amount than it captured (a dispute or conversion adjustment)."""
        with self._lock:
            self._captures[provider_txn_id].settled_amount = amount

    def settlement_rows(self, business_date: date, cutoff: time) -> list[SettlementRow]:
        """The rows of the batch the provider closes at `cutoff` on `business_date`.

        The batch holds everything captured or refunded since the previous day's cutoff, so a capture made after
        the cutoff settles in the next day's batch. Rows are in transaction-id order.
        """
        close = datetime.combine(business_date, cutoff, tzinfo=UTC)
        open_ = close - timedelta(days=1)
        stamp = close + timedelta(minutes=30)
        with self._lock:
            rows: list[SettlementRow] = []
            for capture in sorted(self._captures.values(), key=lambda item: item.txn_id):
                if open_ <= capture.captured_at < close:
                    rows.append(
                        SettlementRow(
                            capture.txn_id, SettlementKind.SETTLED, capture.settled_amount, capture.settled_fee, stamp
                        )
                    )
            for refund in self._refunds:
                if open_ <= refund.refunded_at < close:
                    currency = refund.amount.currency
                    rows.append(
                        SettlementRow(
                            refund.txn_id, SettlementKind.REFUNDED, -refund.amount, Money.zero(currency), stamp
                        )
                    )
            return sorted(rows, key=lambda row: (row.provider_txn_id, row.kind.value))
