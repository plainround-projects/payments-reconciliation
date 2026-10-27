"""The ports: what the use cases need from the outside world, as interfaces the adapters implement."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.ledger import JournalEntry, Leg
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment
from payments_reconciliation.domain.reconciliation import Break
from payments_reconciliation.domain.settlement import SettlementKind


class Clock(Protocol):
    """The current time, injected so runs are deterministic."""

    def now(self) -> datetime:
        """A timezone-aware UTC time."""
        ...


class IdSource(Protocol):
    """New identifiers, injected for the same reason."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        ...


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """The provider captured: its own transaction id, and the fee it charged."""

    provider_txn_id: str
    fee: Money


@dataclass(frozen=True, slots=True)
class RefundResult:
    """The provider returned money for a transaction."""

    provider_refund_id: str


class PaymentProvider(Protocol):
    """The payment provider, as the capture path uses it. Every call carries an idempotency key it honors."""

    def capture(self, *, idempotency_key: str, amount: Money, occurred_at: datetime) -> CaptureResult:
        """Capture `amount`; the same key returns the first result."""
        ...

    def refund(
        self, *, idempotency_key: str, provider_txn_id: str, amount: Money, occurred_at: datetime
    ) -> RefundResult:
        """Refund `amount` of a transaction; the same key returns the first result."""
        ...


class SettlementFiles(Protocol):
    """Where the provider's daily settlement files arrive."""

    def read(self, business_date: date) -> str:
        """The file's text. Raises `SettlementMissingError` when the day has none."""
        ...

    def write(self, business_date: date, text: str) -> None:
        """Store a file (the simulation's stand-in for the provider delivering it)."""
        ...


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One stored posting, as read back."""

    entry_leg: Leg
    account: Account
    amount: Money


@dataclass(frozen=True, slots=True)
class RefundRecord:
    """A refund recorded in the ledger."""

    refund_id: UUID
    payment_id: UUID
    amount: Money
    reference: str
    created_at: datetime


class PaymentRepository(Protocol):
    """Payments, looked up by the provider's transaction id."""

    def add(self, payment: LedgerPayment, idempotency_key: str | None = None) -> None:
        """Insert a payment; a repeated idempotency key violates a unique constraint."""
        ...

    def by_key(self, idempotency_key: str) -> LedgerPayment | None:
        """The payment recorded for a capture request key, if any."""
        ...

    def lock_txn(self, provider_txn_id: str) -> None:
        """Serialize resolvers of one provider transaction until the unit of work ends."""
        ...

    def for_txn(self, provider_txn_id: str) -> list[LedgerPayment]:
        """Every payment (active or reversed) carrying this provider transaction id, oldest first."""
        ...

    def captured_on(self, day: date) -> list[LedgerPayment]:
        """Payments captured on a UTC calendar day, any state."""
        ...

    def by_txns(self, provider_txn_ids: Collection[str]) -> list[LedgerPayment]:
        """Payments for a set of provider transaction ids."""
        ...

    def save(self, payment: LedgerPayment) -> None:
        """Write the changed state, fee and refunded total of an existing payment."""
        ...


class LedgerRepository(Protocol):
    """The append-only ledger."""

    def post(
        self, payment_id: UUID, operation: str, reference: str, entries: Sequence[JournalEntry], at: datetime
    ) -> None:
        """Append whole entries. The store refuses a repeat of the same (payment, operation, reference, leg)."""
        ...

    def rows_for(self, payment_id: UUID) -> list[LedgerRow]:
        """Every posting of one payment, in the order posted."""
        ...

    def total(self, currency: str) -> Money:
        """The sum of every posting; zero whenever the ledger is whole."""
        ...

    def digest(self) -> str:
        """A SHA-256 over every posting in order, to show that a second pass changed nothing."""
        ...


class RefundRepository(Protocol):
    """Refunds recorded in the ledger."""

    def add(self, refund: RefundRecord) -> None:
        """Insert a refund."""
        ...

    def exists(self, payment_id: UUID, reference: str) -> bool:
        """True when this payment already has a refund with this reference."""
        ...

    def recorded_on(self, day: date, currency: str) -> dict[str, Money]:
        """Refunded totals by provider transaction id for refunds recorded on a UTC calendar day."""
        ...


class BreakStatus(StrEnum):
    """Where a break is in its life."""

    OPEN = "OPEN"  # found, a rule can resolve it
    CARRIED = "CARRIED"  # a timing difference waiting for the next report
    AUTO_RESOLVED = "AUTO_RESOLVED"  # cleared by a rule from the report's own number (fees)
    RESOLVED = "RESOLVED"  # fixed in the ledger from the report's data
    NEEDS_REVIEW = "NEEDS_REVIEW"  # nothing can decide: on the worklist
    CLEARED = "CLEARED"  # went away by itself (a timing difference that settled, a break that no longer reproduces)
    ESCALATED = "ESCALATED"  # a timing difference that waited too long; a new break holds it now


@dataclass(frozen=True, slots=True)
class StoredBreak:
    """A break as persisted: the finding plus its status and history."""

    id: int
    run_date: date
    finding: Break
    status: BreakStatus
    note: str
    updated_at: datetime
    cleared_on: date | None = None


class BreakRepository(Protocol):
    """Breaks found by reconciliation runs."""

    def find(self, run_date: date, provider_txn_id: str, kind: SettlementKind, cause: str) -> StoredBreak | None:
        """The stored break with this natural key, if any."""
        ...

    def insert(self, run_date: date, finding: Break, status: BreakStatus, note: str, at: datetime) -> StoredBreak:
        """Store a newly found break."""
        ...

    def get_for_update(self, break_id: int) -> StoredBreak | None:
        """Read and lock one break until the unit of work ends."""
        ...

    def update(
        self, break_id: int, status: BreakStatus, note: str, at: datetime, cleared_on: date | None = None
    ) -> None:
        """Change a break's status and note."""
        ...

    def refresh(self, break_id: int, finding: Break, at: datetime) -> None:
        """Replace the numbers of an unresolved break with this run's findings."""
        ...

    def for_run(self, run_date: date) -> list[StoredBreak]:
        """Every break of one run, in provider-id order."""
        ...

    def carried(self) -> list[StoredBreak]:
        """Timing differences still waiting, oldest first."""
        ...

    def cleared_on(self, day: date) -> list[StoredBreak]:
        """Carried breaks that cleared on this day's run."""
        ...

    def worklist(self) -> list[StoredBreak]:
        """Breaks that need a person, oldest first."""
        ...


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The numbers of one reconciliation run, kept so the report can show the first pass after the fixes."""

    run_date: date
    currency: str
    ledger_payments: int
    settlement_rows: int
    settled_total: Money
    refunded_total: Money
    clean_transactions: int
    clean_total: Money
    break_count: int
    ran_at: datetime


class RunRepository(Protocol):
    """Reconciliation runs."""

    def save(self, run: RunRecord) -> None:
        """Record the first pass of a day; a later run of the same day only counts itself."""
        ...

    def get(self, run_date: date) -> RunRecord | None:
        """The run for a date, if it ran."""
        ...


class UnitOfWork(Protocol):
    """One database transaction: commits when the block ends normally, rolls back on an exception."""

    @property
    def payments(self) -> PaymentRepository:
        """The payments."""
        ...

    @property
    def ledger(self) -> LedgerRepository:
        """The ledger."""
        ...

    @property
    def refunds(self) -> RefundRepository:
        """The refunds."""
        ...

    @property
    def breaks(self) -> BreakRepository:
        """The breaks."""
        ...

    @property
    def runs(self) -> RunRepository:
        """The runs."""
        ...


UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]


class HealthProbe(Protocol):
    """Reports whether a dependency is reachable."""

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        ...
