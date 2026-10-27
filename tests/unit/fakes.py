"""In-memory adapters for the ports, so the use cases are tested without a database.

`InMemoryDatabase.unit_of_work` behaves like a transaction: it holds a lock for the block and restores a snapshot if
the block raises.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from payments_reconciliation.application.ports import (
    BreakStatus,
    LedgerRow,
    RefundRecord,
    RunRecord,
    StoredBreak,
    UnitOfWork,
)
from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.ledger import JournalEntry, Leg
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment
from payments_reconciliation.domain.reconciliation import Break
from payments_reconciliation.domain.settlement import SettlementKind

START = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)


class FixedClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SequentialIds:
    """Predictable UUIDs: 00000000-0000-4000-8000-000000000001, ...0002, ..."""

    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> UUID:
        self._counter += 1
        return UUID(f"00000000-0000-4000-8000-{self._counter:012d}")


class LedgerIntegrityError(Exception):
    """What a unique key in the database would raise."""


class InMemoryDatabase:
    """The state of all repositories, with snapshot-based rollback."""

    def __init__(self) -> None:
        self.payments: dict[UUID, LedgerPayment] = {}
        self.keys: dict[str, UUID] = {}
        self.postings: list[tuple[UUID, str, str, Leg, Account, Money]] = []
        self.refunds: list[RefundRecord] = []
        self.breaks: dict[int, StoredBreak] = {}
        self.runs: dict[date, RunRecord] = {}
        self._next_break = 0
        self._lock = threading.RLock()

    @contextmanager
    def unit_of_work(self) -> Iterator[UnitOfWork]:
        with self._lock:
            # Shallow copies are enough: every stored value is an immutable dataclass or tuple.
            snapshot = (
                dict(self.payments),
                dict(self.keys),
                list(self.postings),
                list(self.refunds),
                dict(self.breaks),
                dict(self.runs),
                self._next_break,
            )
            try:
                yield _InMemoryUnitOfWork(self)
            except BaseException:
                (self.payments, self.keys, self.postings, self.refunds, self.breaks, self.runs, self._next_break) = (
                    snapshot
                )
                raise

    def ledger_sum(self, currency: str = "USD") -> int:
        return sum(row[5].minor for row in self.postings if row[5].currency == currency)

    def postings_of(self, payment_id: UUID) -> list[tuple[Account, int]]:
        return [(row[4], row[5].minor) for row in self.postings if row[0] == payment_id]

    def payments_of(self, provider_txn_id: str) -> list[LedgerPayment]:
        return sorted(
            (p for p in self.payments.values() if p.provider_txn_id == provider_txn_id),
            key=lambda p: (p.captured_at, str(p.id)),
        )

    def break_of(self, provider_txn_id: str, cause: str | None = None) -> list[StoredBreak]:
        return [
            b
            for b in self.breaks.values()
            if b.finding.provider_txn_id == provider_txn_id and (cause is None or b.finding.cause.value == cause)
        ]


class _Payments:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, payment: LedgerPayment, idempotency_key: str | None = None) -> None:
        if idempotency_key is not None:
            if idempotency_key in self._db.keys:
                raise LedgerIntegrityError(f"key {idempotency_key} already used")
            self._db.keys[idempotency_key] = payment.id
        self._db.payments[payment.id] = payment

    def by_key(self, idempotency_key: str) -> LedgerPayment | None:
        payment_id = self._db.keys.get(idempotency_key)
        return None if payment_id is None else self._db.payments[payment_id]

    def lock_txn(self, provider_txn_id: str) -> None:
        return None

    def for_txn(self, provider_txn_id: str) -> list[LedgerPayment]:
        return self._db.payments_of(provider_txn_id)

    def captured_on(self, day: date) -> list[LedgerPayment]:
        start = datetime.combine(day, time(0, 0), tzinfo=UTC)
        end = start + timedelta(days=1)
        found = [p for p in self._db.payments.values() if start <= p.captured_at < end]
        return sorted(found, key=lambda p: (p.provider_txn_id, str(p.id)))

    def by_txns(self, provider_txn_ids: Collection[str]) -> list[LedgerPayment]:
        found = [p for p in self._db.payments.values() if p.provider_txn_id in provider_txn_ids]
        return sorted(found, key=lambda p: (p.provider_txn_id, p.captured_at, str(p.id)))

    def save(self, payment: LedgerPayment) -> None:
        self._db.payments[payment.id] = payment


class _Ledger:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def post(
        self, payment_id: UUID, operation: str, reference: str, entries: Sequence[JournalEntry], at: datetime
    ) -> None:
        for entry in entries:
            if any(row[:4] == (payment_id, operation, reference, entry.leg) for row in self._db.postings):
                raise LedgerIntegrityError(f"{operation} {reference} {entry.leg} already posted for {payment_id}")
            for posting in entry.postings():
                self._db.postings.append((payment_id, operation, reference, entry.leg, posting.account, posting.amount))

    def rows_for(self, payment_id: UUID) -> list[LedgerRow]:
        return [LedgerRow(row[3], row[4], row[5]) for row in self._db.postings if row[0] == payment_id]

    def total(self, currency: str) -> Money:
        return Money(self._db.ledger_sum(currency), currency)

    def digest(self) -> str:
        digest = hashlib.sha256()
        for row in self._db.postings:
            digest.update(f"{row[0]}|{row[4].value}|{row[5].minor}|{row[5].currency}\n".encode())
        return digest.hexdigest()


class _Refunds:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def add(self, refund: RefundRecord) -> None:
        if self.exists(refund.payment_id, refund.reference):
            raise LedgerIntegrityError("refund reference already used")
        self._db.refunds.append(refund)

    def exists(self, payment_id: UUID, reference: str) -> bool:
        return any(r.payment_id == payment_id and r.reference == reference for r in self._db.refunds)

    def recorded_on(self, day: date, currency: str) -> dict[str, Money]:
        start = datetime.combine(day, time(0, 0), tzinfo=UTC)
        end = start + timedelta(days=1)
        totals: dict[str, Money] = {}
        for refund in self._db.refunds:
            if start <= refund.created_at < end and refund.amount.currency == currency:
                txn = self._db.payments[refund.payment_id].provider_txn_id
                totals[txn] = totals.get(txn, Money.zero(currency)) + refund.amount
        return totals


class _Breaks:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def find(self, run_date: date, provider_txn_id: str, kind: SettlementKind, cause: str) -> StoredBreak | None:
        for stored in self._db.breaks.values():
            finding = stored.finding
            if (stored.run_date, finding.provider_txn_id, finding.kind, finding.cause.value) == (
                run_date,
                provider_txn_id,
                kind,
                cause,
            ):
                return stored
        return None

    def insert(self, run_date: date, finding: Break, status: BreakStatus, note: str, at: datetime) -> StoredBreak:
        if self.find(run_date, finding.provider_txn_id, finding.kind, finding.cause.value) is not None:
            raise LedgerIntegrityError("break already stored")
        self._db._next_break += 1
        stored = StoredBreak(self._db._next_break, run_date, finding, status, note, at)
        self._db.breaks[stored.id] = stored
        return stored

    def get_for_update(self, break_id: int) -> StoredBreak | None:
        return self._db.breaks.get(break_id)

    def update(
        self, break_id: int, status: BreakStatus, note: str, at: datetime, cleared_on: date | None = None
    ) -> None:
        self._db.breaks[break_id] = replace(
            self._db.breaks[break_id], status=status, note=note, updated_at=at, cleared_on=cleared_on
        )

    def refresh(self, break_id: int, finding: Break, at: datetime) -> None:
        self._db.breaks[break_id] = replace(self._db.breaks[break_id], finding=finding, updated_at=at)

    def for_run(self, run_date: date) -> list[StoredBreak]:
        found = [b for b in self._db.breaks.values() if b.run_date == run_date]
        return sorted(found, key=lambda b: (b.finding.provider_txn_id, b.finding.kind.value, b.finding.cause.value))

    def carried(self) -> list[StoredBreak]:
        found = [b for b in self._db.breaks.values() if b.status is BreakStatus.CARRIED]
        return sorted(found, key=lambda b: (b.run_date, b.finding.provider_txn_id))

    def cleared_on(self, day: date) -> list[StoredBreak]:
        found = [b for b in self._db.breaks.values() if b.cleared_on == day]
        return sorted(found, key=lambda b: b.finding.provider_txn_id)

    def worklist(self) -> list[StoredBreak]:
        found = [b for b in self._db.breaks.values() if b.status is BreakStatus.NEEDS_REVIEW]
        return sorted(found, key=lambda b: (b.run_date, b.finding.provider_txn_id, b.finding.kind.value))


class _Runs:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def save(self, run: RunRecord) -> None:
        self._db.runs.setdefault(run.run_date, run)  # the first pass is kept

    def get(self, run_date: date) -> RunRecord | None:
        return self._db.runs.get(run_date)


class _InMemoryUnitOfWork:
    def __init__(self, db: InMemoryDatabase) -> None:
        self.payments = _Payments(db)
        self.ledger = _Ledger(db)
        self.refunds = _Refunds(db)
        self.breaks = _Breaks(db)
        self.runs = _Runs(db)


def new_id() -> UUID:
    return uuid.uuid4()
