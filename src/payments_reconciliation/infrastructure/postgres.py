"""PostgreSQL: the connection pool, the unit of work, and the repositories behind the ports."""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from payments_reconciliation.application.ports import (
    BreakStatus,
    IdSource,
    LedgerRow,
    RefundRecord,
    RunRecord,
    StoredBreak,
    UnitOfWork,
)
from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.ledger import JournalEntry, Leg
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState
from payments_reconciliation.domain.reconciliation import Break, Cause
from payments_reconciliation.domain.settlement import SettlementKind
from payments_reconciliation.infrastructure.settings import PostgresSettings

_PAYMENT = "id, provider_txn_id, currency, amount_minor, fee_minor, state, source, refunded_minor, captured_at"
_BREAK = (
    "id, run_date, provider_txn_id, kind, cause, status, currency, ledger_amount_minor, settlement_amount_minor, "
    "ledger_fee_minor, settlement_fee_minor, detail, note, cleared_on, updated_at"
)


def conninfo(settings: PostgresSettings) -> str:
    """The libpq connection string for `settings`; sessions run in UTC so dates are UTC calendar days."""
    return make_conninfo(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.user,
        password=settings.password,
        connect_timeout=settings.connect_timeout_seconds,
        application_name="payments-reconciliation",
        options="-c timezone=UTC",
    )


def open_pool(settings: PostgresSettings) -> ConnectionPool[psycopg.Connection[Any]]:
    """Open a pool and wait until it holds a working connection, or fail within the connect timeout."""
    pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(
        conninfo(settings), min_size=1, max_size=settings.pool_max_size, open=False, timeout=10.0
    )
    pool.open(wait=True, timeout=float(settings.connect_timeout_seconds) * 6)
    return pool


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=UTC)
    return start, start + timedelta(days=1)


class PostgresPayments:
    """`PaymentRepository` over the `payments` table."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, payment: LedgerPayment, idempotency_key: str | None = None) -> None:
        """Insert a payment."""
        self._connection.execute(
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, "
            "refunded_minor, idempotency_key, captured_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                payment.id,
                payment.provider_txn_id,
                payment.amount.currency,
                payment.amount.minor,
                payment.fee.minor,
                payment.state.value,
                payment.source.value,
                payment.refunded.minor,
                idempotency_key,
                payment.captured_at,
            ),
        )

    def by_key(self, idempotency_key: str) -> LedgerPayment | None:
        """The payment recorded for a capture request key, if any."""
        rows = self._query(f"SELECT {_PAYMENT} FROM payments WHERE idempotency_key = %s", (idempotency_key,))
        return rows[0] if rows else None

    def lock_txn(self, provider_txn_id: str) -> None:
        """Serialize resolvers of one provider transaction until the transaction ends."""
        self._connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (provider_txn_id,))

    def for_txn(self, provider_txn_id: str) -> list[LedgerPayment]:
        """Every payment carrying this provider transaction id, oldest first."""
        return self._query(
            f"SELECT {_PAYMENT} FROM payments WHERE provider_txn_id = %s ORDER BY captured_at, id", (provider_txn_id,)
        )

    def captured_on(self, day: date) -> list[LedgerPayment]:
        """Payments captured on a UTC calendar day, any state."""
        start, end = _day_bounds(day)
        return self._query(
            f"SELECT {_PAYMENT} FROM payments WHERE captured_at >= %s AND captured_at < %s "
            "ORDER BY provider_txn_id, id",
            (start, end),
        )

    def by_txns(self, provider_txn_ids: Collection[str]) -> list[LedgerPayment]:
        """Payments for a set of provider transaction ids."""
        if not provider_txn_ids:
            return []
        return self._query(
            f"SELECT {_PAYMENT} FROM payments WHERE provider_txn_id = ANY(%s) "
            "ORDER BY provider_txn_id, captured_at, id",
            (list(provider_txn_ids),),
        )

    def save(self, payment: LedgerPayment) -> None:
        """Write the changed state, fee and refunded total."""
        self._connection.execute(
            "UPDATE payments SET state = %s, fee_minor = %s, refunded_minor = %s WHERE id = %s",
            (payment.state.value, payment.fee.minor, payment.refunded.minor, payment.id),
        )

    def _query(self, sql: str, params: Sequence[Any]) -> list[LedgerPayment]:
        rows = self._connection.execute(sql, params).fetchall()
        return [
            LedgerPayment(
                id=pid,
                provider_txn_id=txn,
                amount=Money(amount, currency.strip()),
                fee=Money(fee, currency.strip()),
                state=PaymentState(state),
                source=PaymentSource(source),
                captured_at=captured_at,
                refunded=Money(refunded, currency.strip()),
            )
            for pid, txn, currency, amount, fee, state, source, refunded, captured_at in rows
        ]


class PostgresLedger:
    """`LedgerRepository` over `journal_entries` and `postings`."""

    def __init__(self, connection: psycopg.Connection[Any], ids: IdSource) -> None:
        self._connection = connection
        self._ids = ids

    def post(
        self, payment_id: UUID, operation: str, reference: str, entries: Sequence[JournalEntry], at: datetime
    ) -> None:
        """Append whole entries; the unique key on (payment, operation, reference, leg) refuses a repeat.

        The inserts go out in one pipeline, so a capture's nine statements cost one round trip.
        """
        with self._connection.pipeline():
            for entry in entries:
                entry_id = self._ids.new_id()
                self._connection.execute(
                    "INSERT INTO journal_entries (id, payment_id, operation, reference, leg, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (entry_id, payment_id, operation, reference, entry.leg.value, at),
                )
                for posting in entry.postings():
                    self._connection.execute(
                        "INSERT INTO postings (entry_id, payment_id, account, amount_minor, currency, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            entry_id,
                            payment_id,
                            posting.account.value,
                            posting.amount.minor,
                            posting.amount.currency,
                            at,
                        ),
                    )

    def rows_for(self, payment_id: UUID) -> list[LedgerRow]:
        """Every posting of one payment, in the order posted."""
        rows = self._connection.execute(
            "SELECT e.leg, p.account, p.amount_minor, p.currency FROM postings p "
            "JOIN journal_entries e ON e.id = p.entry_id WHERE p.payment_id = %s ORDER BY p.id",
            (payment_id,),
        ).fetchall()
        return [
            LedgerRow(Leg(leg), Account(account), Money(minor, currency.strip()))
            for leg, account, minor, currency in rows
        ]

    def total(self, currency: str) -> Money:
        """The sum of every posting in `currency`."""
        row = self._connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0)::bigint FROM postings WHERE currency = %s", (currency,)
        ).fetchone()
        assert row is not None
        return Money(row[0], currency)

    def digest(self) -> str:
        """A SHA-256 over every posting in insertion order."""
        digest = hashlib.sha256()
        cursor = self._connection.execute(
            "SELECT payment_id, account, amount_minor, currency FROM postings ORDER BY id"
        )
        for payment_id, account, amount, currency in cursor:
            digest.update(f"{payment_id}|{account}|{amount}|{currency}\n".encode())
        return digest.hexdigest()


class PostgresRefunds:
    """`RefundRepository` over `refunds`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, refund: RefundRecord) -> None:
        """Insert a refund."""
        self._connection.execute(
            "INSERT INTO refunds (id, payment_id, amount_minor, currency, reference, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                refund.refund_id,
                refund.payment_id,
                refund.amount.minor,
                refund.amount.currency,
                refund.reference,
                refund.created_at,
            ),
        )

    def exists(self, payment_id: UUID, reference: str) -> bool:
        """True when this payment already has a refund with this reference."""
        row = self._connection.execute(
            "SELECT 1 FROM refunds WHERE payment_id = %s AND reference = %s", (payment_id, reference)
        ).fetchone()
        return row is not None

    def recorded_on(self, day: date, currency: str) -> dict[str, Money]:
        """Refunded totals by provider transaction id for refunds recorded on a UTC calendar day."""
        start, end = _day_bounds(day)
        rows = self._connection.execute(
            "SELECT p.provider_txn_id, sum(r.amount_minor)::bigint FROM refunds r "
            "JOIN payments p ON p.id = r.payment_id "
            "WHERE r.created_at >= %s AND r.created_at < %s AND r.currency = %s GROUP BY p.provider_txn_id",
            (start, end, currency),
        ).fetchall()
        return {txn: Money(total, currency) for txn, total in rows}


class PostgresBreaks:
    """`BreakRepository` over `reconciliation_breaks`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def find(self, run_date: date, provider_txn_id: str, kind: SettlementKind, cause: str) -> StoredBreak | None:
        """The stored break with this natural key, if any."""
        rows = self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE run_date = %s AND provider_txn_id = %s "
            "AND kind = %s AND cause = %s",
            (run_date, provider_txn_id, kind.value, cause),
        )
        return rows[0] if rows else None

    def insert(self, run_date: date, finding: Break, status: BreakStatus, note: str, at: datetime) -> StoredBreak:
        """Store a newly found break."""
        currency = (finding.ledger_amount or finding.settlement_amount or Money.zero("USD")).currency
        row = self._connection.execute(
            "INSERT INTO reconciliation_breaks (run_date, provider_txn_id, kind, cause, status, currency, "
            "ledger_amount_minor, settlement_amount_minor, ledger_fee_minor, settlement_fee_minor, detail, note, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                run_date,
                finding.provider_txn_id,
                finding.kind.value,
                finding.cause.value,
                status.value,
                currency,
                _minor(finding.ledger_amount),
                _minor(finding.settlement_amount),
                _minor(finding.ledger_fee),
                _minor(finding.settlement_fee),
                finding.detail,
                note,
                at,
                at,
            ),
        ).fetchone()
        assert row is not None
        return StoredBreak(row[0], run_date, finding, status, note, at)

    def get_for_update(self, break_id: int) -> StoredBreak | None:
        """Read and lock one break until the transaction ends."""
        rows = self._read(f"SELECT {_BREAK} FROM reconciliation_breaks WHERE id = %s FOR UPDATE", (break_id,))
        return rows[0] if rows else None

    def update(
        self, break_id: int, status: BreakStatus, note: str, at: datetime, cleared_on: date | None = None
    ) -> None:
        """Change a break's status and note."""
        self._connection.execute(
            "UPDATE reconciliation_breaks SET status = %s, note = %s, updated_at = %s, cleared_on = %s WHERE id = %s",
            (status.value, note, at, cleared_on, break_id),
        )

    def refresh(self, break_id: int, finding: Break, at: datetime) -> None:
        """Replace the numbers of an unresolved break with this run's findings."""
        self._connection.execute(
            "UPDATE reconciliation_breaks SET ledger_amount_minor = %s, settlement_amount_minor = %s, "
            "ledger_fee_minor = %s, settlement_fee_minor = %s, detail = %s, updated_at = %s WHERE id = %s",
            (
                _minor(finding.ledger_amount),
                _minor(finding.settlement_amount),
                _minor(finding.ledger_fee),
                _minor(finding.settlement_fee),
                finding.detail,
                at,
                break_id,
            ),
        )

    def for_run(self, run_date: date) -> list[StoredBreak]:
        """Every break of one run, in provider-id order."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE run_date = %s ORDER BY provider_txn_id, kind, cause",
            (run_date,),
        )

    def carried(self) -> list[StoredBreak]:
        """Timing differences still waiting, oldest first."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE status = 'CARRIED' ORDER BY run_date, provider_txn_id",
            (),
        )

    def cleared_on(self, day: date) -> list[StoredBreak]:
        """Carried breaks that cleared on this day's run."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE cleared_on = %s ORDER BY provider_txn_id", (day,)
        )

    def worklist(self) -> list[StoredBreak]:
        """Breaks that need a person, oldest first."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE status = 'NEEDS_REVIEW' "
            "ORDER BY run_date, provider_txn_id, kind",
            (),
        )

    def _read(self, sql: str, params: Sequence[Any]) -> list[StoredBreak]:
        rows = self._connection.execute(sql, params).fetchall()
        result = []
        for (bid, run_date, txn, kind, cause, status, currency, ledger, settled, ledger_fee, settled_fee, detail, note,
             cleared_on, updated_at) in rows:  # fmt: skip
            currency = currency.strip()
            finding = Break(
                txn,
                SettlementKind(kind),
                Cause(cause),
                _money(ledger, currency),
                _money(settled, currency),
                _money(ledger_fee, currency),
                _money(settled_fee, currency),
                detail,
            )
            result.append(StoredBreak(bid, run_date, finding, BreakStatus(status), note, updated_at, cleared_on))
        return result


class PostgresRuns:
    """`RunRepository` over `reconciliation_runs`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def save(self, run: RunRecord) -> None:
        """Record the first pass of a day; a later run of the same day only counts itself."""
        self._connection.execute(
            "INSERT INTO reconciliation_runs (run_date, currency, ledger_payments, settlement_rows, "
            "settled_total_minor, refunded_total_minor, clean_transactions, clean_total_minor, break_count, ran_at, "
            "last_ran_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_date) DO UPDATE SET "
            "reruns = reconciliation_runs.reruns + 1, last_ran_at = excluded.last_ran_at",
            (
                run.run_date,
                run.currency,
                run.ledger_payments,
                run.settlement_rows,
                run.settled_total.minor,
                run.refunded_total.minor,
                run.clean_transactions,
                run.clean_total.minor,
                run.break_count,
                run.ran_at,
                run.ran_at,
            ),
        )

    def get(self, run_date: date) -> RunRecord | None:
        """The run for a date, if it ran."""
        row = self._connection.execute(
            "SELECT currency, ledger_payments, settlement_rows, settled_total_minor, refunded_total_minor, "
            "clean_transactions, clean_total_minor, break_count, ran_at FROM reconciliation_runs WHERE run_date = %s",
            (run_date,),
        ).fetchone()
        if row is None:
            return None
        currency, payments, rows, settled, refunded, clean, clean_total, breaks, ran_at = row
        currency = currency.strip()
        return RunRecord(
            run_date, currency, payments, rows, Money(settled, currency), Money(refunded, currency), clean,
            Money(clean_total, currency), breaks, ran_at,
        )  # fmt: skip


def _minor(amount: Money | None) -> int | None:
    return None if amount is None else amount.minor


def _money(minor: int | None, currency: str) -> Money | None:
    return None if minor is None else Money(minor, currency)


@dataclass(slots=True)
class PostgresUnitOfWork:
    """The repositories of one connection, all inside the transaction the pool opened for it."""

    payments: PostgresPayments
    ledger: PostgresLedger
    refunds: PostgresRefunds
    breaks: PostgresBreaks
    runs: PostgresRuns


class PostgresUnitOfWorkFactory:
    """Open a transaction on a pooled connection: commit on a normal exit, roll back on an exception."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]], ids: IdSource) -> None:
        self._pool = pool
        self._ids = ids

    @contextmanager
    def __call__(self) -> Iterator[UnitOfWork]:
        """One unit of work."""
        with self._pool.connection() as connection:
            yield PostgresUnitOfWork(
                PostgresPayments(connection),
                PostgresLedger(connection, self._ids),
                PostgresRefunds(connection),
                PostgresBreaks(connection),
                PostgresRuns(connection),
            )


class PostgresHealth:
    """`HealthProbe`: can a pooled connection run `SELECT 1`?"""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
        self._pool = pool

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        with self._pool.connection(timeout=2.0) as connection:
            connection.execute("SELECT 1")
