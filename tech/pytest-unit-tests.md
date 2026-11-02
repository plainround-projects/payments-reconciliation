# pytest walkthrough: the unit tests, line by line

## What it is

This note reads every line of the tests that need no database: the shared fixtures, the in-memory fakes, and the tests of the domain, the use cases, the infrastructure helpers and the HTTP and command-line layers. For the general idea of pytest see `pytest.md`.

A **unit test** checks one small piece in isolation. Here the "isolation" is a set of **fakes**: simple Python objects that behave like the database and the clock but keep everything in memory, so a test runs in milliseconds and gives the same result every time. The root `conftest.py` is included here because it holds the fixtures the database tests share.

## Why this repository uses it

The lesson's promises (every difference is found and labeled by its own cause, resolving twice changes nothing, the report balances) must be checked after every change. Unit tests check them in seconds without Docker, so the first command a developer runs (`make check`) can include them, together with the 95 percent coverage bar on the domain and application.

## Where it appears

| File | What it is |
| --- | --- |
| `tests/conftest.py` | Root fixtures: the settings, the wait for the database and the schema loader. |
| `tests/unit/conftest.py` | Unit fixtures: the in-memory world. |
| `tests/unit/fakes.py` | In-memory stand-ins for the database, clock and ids. |
| `tests/unit/domain/test_money_and_fees.py` | Tests of Money and FeePolicy. |
| `tests/unit/domain/test_ledger.py` | Tests of the ledger rules. |
| `tests/unit/domain/test_settlement.py` | Tests of the settlement file format. |
| `tests/unit/domain/test_reconciliation.py` | Tests of matching and classification. |
| `tests/unit/application/test_lesson_scenario.py` | The lesson's scenario on the in-memory ledger. |
| `tests/unit/application/test_report_and_capture.py` | The report text and the capture path. |
| `tests/unit/infrastructure/test_logging_retry.py` | Log redaction and the retry helper. |
| `tests/unit/infrastructure/test_provider_files_settings.py` | Stub provider, settlement files and settings. |
| `tests/unit/interfaces/test_http_and_cli.py` | The HTTP layer and the command-line handlers. |

## How it works underneath

pytest imports each `test_*.py` file and every `conftest.py` above it. A test function's parameters are matched by name with **fixtures**; pytest calls the fixture and passes the result in. The functions use plain `assert`; when an assertion is false pytest shows the values on both sides. The in-memory `unit_of_work` copies the state before a block and puts it back if the block raises, which is what a database rollback does. The simulator makes the six differences happen on demand, using a seeded random generator so a day is repeatable.

## Walkthrough of this repository's use

Files are shown in the order they are used: fixtures and fakes first, then the tests, from the innermost layer to the outermost.

### `tests/conftest.py` (49 lines)

```python
"""Fixtures for the tests that need the PostgreSQL container from docker-compose.yml (`make up`)."""

from __future__ import annotations

import pathlib
import time
from typing import Any

import psycopg
import pytest

from payments_reconciliation.infrastructure.postgres import conninfo
from payments_reconciliation.infrastructure.settings import Settings

SQL = pathlib.Path(__file__).resolve().parent.parent / "sql"
```

Fixtures for the tests that need the PostgreSQL container from `docker-compose.yml` (`make up`). `time` is used for waiting; `pathlib.Path` represents file paths; `SQL` is the folder `sql/` found relative to this file (`Path(__file__).resolve().parent.parent / "sql"`).

```python
@pytest.fixture(scope="session")
def settings() -> Settings:
    """From the environment (make loads .env; CI sets the variables); the password has no default."""
    return Settings.from_env()
```

`settings` reads the real settings once per test run (`scope="session"`). The docstring notes that make loads `.env` and CI sets the variables, and that the password has no default.

```python
def wait_until_ready(settings: Any, seconds: float = 180.0) -> None:
    """Wait for the database to accept connections: it may be starting, or recovering after a starved container."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            with psycopg.connect(conninfo(settings.postgres), connect_timeout=5) as connection:
                connection.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.0)
```

Waits up to 180 seconds for the database to accept connections: it may be starting, or recovering after the container was starved of resources. It loops with `while True`: try to connect and run `SELECT 1`, and `return` on success; on `psycopg.OperationalError` (the database is not ready) it raises the error again if the deadline has passed (`time.monotonic()` is a clock that never jumps), otherwise sleeps one second and tries again.

```python
def load_schema(connection: psycopg.Connection[Any]) -> None:
    """Drop everything and apply 001_schema.sql and 002_seed.sql: the same as `make db`."""
    for script in ("reset.sql", "001_schema.sql", "002_seed.sql"):
        connection.execute((SQL / script).read_text())
```

`load_schema` runs the three SQL files in order (`reset.sql`, `001_schema.sql`, `002_seed.sql`) by reading each file's text and executing it: the same as `make db`.

```python
@pytest.fixture(scope="session")
def database(settings: Settings) -> None:
    """A fresh schema with the seed, once per test session. This drops whatever the database holds."""
    wait_until_ready(settings)
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as connection:
        load_schema(connection)
```

A session-scoped fixture (the `@pytest.fixture(scope="session")` line above it): it first waits until the database is ready, then connects with `autocommit=True` (each statement applies at once) and loads the schema and seed once per run. The docstring warns that it drops whatever the database holds.

### `tests/unit/conftest.py` (49 lines)

```python
"""Fixtures: the service wired to in-memory storage, the stub provider and a temporary settlement folder."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

import pytest

from payments_reconciliation.infrastructure.settings import Settings
from payments_reconciliation.infrastructure.simulation import SimulatedClock
from payments_reconciliation.interfaces.container import Container, assemble
from tests.unit.fakes import START, FixedClock, InMemoryDatabase, SequentialIds
```

Shared test setup (a **fixture** file: pytest reads `conftest.py` automatically). The docstring: the service wired to in-memory storage, the stub provider and a temporary settlement folder. `pathlib` represents folders; `START` is the fixed moment the clock starts at.

```python
@dataclass
class World:
    """Everything a test needs: the use cases, the storage to look into, the clocks."""

    services: Container
    db: InMemoryDatabase
    clock: FixedClock
    settings: Settings
```

`World` bundles everything a test needs: the use-case container, the in-memory database to look into, the clock and the settings.

```python
def build_world(directory: pathlib.Path, *, max_carry_days: int = 2) -> World:
    settings = Settings.from_env(
        {
            "POSTGRES_PASSWORD": "not-a-real-secret",
            "SETTLEMENT_DIR": str(directory),
            "MAX_CARRY_DAYS": str(max_carry_days),
        }
    )
    db = InMemoryDatabase()
    clock = FixedClock()
    services = assemble(
        settings=settings,
        uow_factory=db.unit_of_work,
        clock=clock,
        simulation_clock=SimulatedClock(START),
        ids=SequentialIds(),
        probes={},
    )
    return World(services, db, clock, settings)
```

`build_world` assembles it for a given folder: settings read from a plain dictionary (a fake password, the folder as `SETTLEMENT_DIR`, and `MAX_CARRY_DAYS`), a fresh database and clock, and `assemble(...)` from the real composition root with the in-memory unit of work as the transaction factory, a simulated clock starting at `START` and sequential ids. It is a plain function so a test can call it with another folder or another carry limit.

```python
@pytest.fixture
def world(tmp_path: pathlib.Path) -> World:
    return build_world(tmp_path)
```

`@pytest.fixture` marks `world(tmp_path)` as a **fixture**: a test with a parameter named `world` receives a fresh `World`; `tmp_path` is pytest's own fixture giving a new empty temporary folder for each test.

### `tests/unit/fakes.py` (277 lines)

```python
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
```

**Fakes** are simple stand-ins for real parts, used so the use cases can be tested without a database. The docstring explains the central one: `InMemoryDatabase.unit_of_work` behaves like a transaction. It holds a lock for the block and restores a snapshot if the block raises, which imitates a rollback. `START` is a fixed moment (`datetime(2026, 10, 27, 6, 0, tzinfo=UTC)`) used as "now" so no test depends on today's date.

```python
class FixedClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta
```

`FixedClock` implements the `Clock` port: `now()` returns the same time until a test calls `advance(delta)`, which moves it forward with `+=` on a `timedelta`. That makes expiry tests exact.

```python
class SequentialIds:
    """Predictable UUIDs: 00000000-0000-4000-8000-000000000001, ...0002, ..."""

    def __init__(self) -> None:
        self._counter = 0

    def new_id(self) -> UUID:
        self._counter += 1
        return UUID(f"00000000-0000-4000-8000-{self._counter:012d}")
```

`SequentialIds` implements `IdSource` with predictable ids: a counter increases on each call and the UUID is built from it with `f"...{self._counter:012d}"` (`:012d` pads the number with zeros to 12 digits), giving `00000000-0000-4000-8000-000000000001`, then `...0002` and so on.

```python
class LedgerIntegrityError(Exception):
    """What a unique key in the database would raise."""
```

A stand-in for the error the real database would raise when a unique key is violated.

```python
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
```

The state of all repositories as plain Python containers: dictionaries of payments, keys and breaks, lists of postings and refunds, a dictionary of runs, a counter for break ids and a re-entrant lock.

```python
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
```

`unit_of_work` is a context manager. Under the lock it takes a snapshot of the whole state (copies of the dictionaries and lists; the comment explains that **shallow** copies are enough because every stored value is an immutable dataclass or tuple), yields a unit of work to the test code, and if the block raises *anything* (`except BaseException`) puts the snapshot back (the tuple assignment restores all seven values at once) and re-raises. That is what a database rollback does.

```python
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
```

Test helpers: `ledger_sum` adds up every stored posting in a currency; `postings_of(payment_id)` lists a payment's `(account, minor units)` pairs; `payments_of(txn)` lists the payments with one provider id, oldest first; `break_of(txn, cause)` lists the stored breaks of a transaction, optionally for one cause (`cause is None or ...`).

```python
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
```

The in-memory payment repository. `add` refuses a repeated idempotency key (raising `LedgerIntegrityError`) and stores the payment; `by_key` looks a payment up by key (`None if payment_id is None else ...` is a one-line `if`/`else`); `lock_txn` does nothing (the whole database is locked); `for_txn`, `captured_on` and `by_txns` filter and sort like the SQL; `save` stores a changed payment.

```python
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
```

The in-memory ledger. `post` refuses a repeat of the same payment, operation, reference and leg (`any(row[:4] == (...) for row in ...)` compares the first four items of each stored tuple) by raising `LedgerIntegrityError`, imitating the database's unique key; otherwise it appends one tuple per posting. `rows_for` reads a payment's rows back, `total` sums a currency, and `digest` makes a SHA-256 over the postings just like the PostgreSQL version.

```python
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
```

The in-memory refunds. `add` refuses a repeated reference, `exists` checks for one, and `recorded_on` sums the refunds created on a UTC day per provider transaction id.

```python
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
```

The in-memory breaks, behaving like the SQL version: `find` looks for the natural key (the tuple comparison compares four values at once); `insert` refuses a duplicate key and assigns the next id; `get_for_update` reads; `update` and `refresh` replace the stored break using `dataclasses.replace`; `for_run`, `carried`, `cleared_on` and `worklist` filter and sort like the queries.

```python
class _Runs:
    def __init__(self, db: InMemoryDatabase) -> None:
        self._db = db

    def save(self, run: RunRecord) -> None:
        self._db.runs.setdefault(run.run_date, run)  # the first pass is kept

    def get(self, run_date: date) -> RunRecord | None:
        return self._db.runs.get(run_date)
```

The in-memory runs. `setdefault(run.run_date, run)` stores a run only if the date has none, which keeps the first pass, as the SQL `ON CONFLICT` does.

```python
class _InMemoryUnitOfWork:
    def __init__(self, db: InMemoryDatabase) -> None:
        self.payments = _Payments(db)
        self.ledger = _Ledger(db)
        self.refunds = _Refunds(db)
        self.breaks = _Breaks(db)
        self.runs = _Runs(db)
```

Bundles the five repositories.

```python
def new_id() -> UUID:
    return uuid.uuid4()
```

A helper returning a random UUID.

### `tests/unit/domain/test_money_and_fees.py` (85 lines)

```python
import pytest

from payments_reconciliation.domain.errors import CurrencyMismatchError, InvalidMoneyError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money, sum_money
```

Tests of `Money` and `FeePolicy`. A test is a function whose name starts with `test_`; it passes if it finishes without raising an exception. `assert` raises `AssertionError` when its condition is false. `pytest.raises(SomeError)` is a block that passes only if the code inside raises that error.

```python
def test_parse_reads_dollars_into_exact_cents():
    assert Money.parse("42.50", "USD") == Money(4250, "USD")
```

Reading `"42.50"` gives exactly 4250 cents.

```python
def test_parse_keeps_a_whole_number_of_yen_as_is():
    assert Money.parse("500", "JPY") == Money(500, "JPY")
```

Yen have no minor units, so `"500"` is 500.

```python
@pytest.mark.parametrize("text", ["42.505", "abc", "NaN", "Infinity", ""])
def test_parse_refuses_what_is_not_an_exact_amount(text):
    with pytest.raises(InvalidMoneyError):
        Money.parse(text, "USD")
```

`@pytest.mark.parametrize` runs one test body once per listed value. These five texts (too many decimals, not a number, NaN, infinity, empty) must all raise `InvalidMoneyError`.

```python
def test_parse_refuses_decimals_in_a_currency_without_minor_units():
    with pytest.raises(InvalidMoneyError):
        Money.parse("5.5", "JPY")
```

`"5.5"` yen is refused.

```python
def test_an_unknown_currency_is_refused():
    with pytest.raises(InvalidMoneyError):
        Money(100, "XXX")
    with pytest.raises(InvalidMoneyError):
        Money.parse("1.00", "XXX")
```

An unsupported currency code fails both in the constructor and in `parse`.

```python
@pytest.mark.parametrize("minor", [1.5, "100", True])
def test_minor_units_must_be_an_integer(minor):
    with pytest.raises(InvalidMoneyError):
        Money(minor, "USD")
```

Only a real integer is accepted as an amount: a float, a text and a `bool` are refused.

```python
@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [(4250, "USD", "42.50"), (5, "USD", "0.05"), (0, "USD", "0.00"), (-123, "USD", "-1.23"), (500, "JPY", "500")],
)
def test_format_uses_the_currencys_own_places(minor, currency, text):
    assert Money(minor, currency).format() == text
```

`format` is checked for five (minor, currency, text) cases, including a negative amount and yen.

```python
def test_arithmetic_and_comparison_stay_in_integers():
    a, b = Money(4250, "USD"), Money(123, "USD")
    assert a - b == Money(4127, "USD")
    assert a + b == Money(4373, "USD")
    assert -a == Money(-4250, "USD")
    assert b < a and b <= a and a > b and a >= b and not a < b
    assert a.is_positive() and not Money.zero("USD").is_positive() and Money.zero("USD").is_zero()
```

Subtraction, addition, negation and all four comparisons work and give exact integer results; `is_positive` and `is_zero` answer correctly.

```python
def test_combining_two_currencies_is_an_error():
    with pytest.raises(CurrencyMismatchError):
        Money(1, "USD") + Money(1, "EUR")
    with pytest.raises(CurrencyMismatchError):
        assert Money(1, "USD") < Money(1, "EUR")
```

Adding or comparing USD with EUR raises `CurrencyMismatchError`.

```python
def test_sum_money_of_nothing_is_zero_in_the_given_currency():
    assert sum_money([], "EUR") == Money(0, "EUR")
    assert sum_money([Money(1, "USD"), Money(2, "USD")], "USD") == Money(3, "USD")
```

The empty sum is zero in the requested currency; a normal sum adds up.

```python
def test_the_worked_example_fee_is_1_23_on_42_50():
    assert FeePolicy(290).fee_for(Money(4250, "USD")) == Money(123, "USD")
```

The lesson's number: 290 basis points on 42.50 is 1.23.

```python
def test_fees_round_half_up_to_a_minor_unit():
    assert FeePolicy(250).fee_for(Money(20, "USD")) == Money(1, "USD")  # exactly half a cent rounds up
    assert FeePolicy(100).fee_for(Money(149, "USD")) == Money(1, "USD")
```

Rounding: 2.5 percent of 20 cents is exactly half a cent and rounds up to 1; 1 percent of 149 cents is 1.49, which rounds to 1.

```python
def test_a_fee_policy_outside_zero_to_ten_thousand_basis_points_is_refused():
    with pytest.raises(InvalidMoneyError):
        FeePolicy(10_001)
```

10,001 basis points is more than 100 percent and is refused.

```python
def test_a_fee_is_charged_on_a_positive_amount_only():
    with pytest.raises(InvalidMoneyError):
        FeePolicy(290).fee_for(Money(0, "USD"))
```

A fee on zero cents is refused.

### `tests/unit/domain/test_ledger.py` (90 lines)

```python
import pytest

from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.errors import UnbalancedEntryError
from payments_reconciliation.domain.ledger import (
    JournalEntry,
    Leg,
    balances,
    capture_entries,
    entries_postings,
    fee_correction_entry,
    ledger_sum,
    refund_entries,
    reversal_entries,
)
from payments_reconciliation.domain.money import Money

USD = "USD"
```

Tests of the ledger rules, including reversals and fee corrections. The imports bring in the entry builders and `balances`; `USD = "USD"` is a shorthand.

```python
def test_the_worked_example_capture_is_six_rows_that_sum_to_zero():
    postings = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    assert [(p.account.value, p.amount.minor) for p in postings] == [
        ("customer_receivable", 4250),
        ("merchant_pending", -4250),
        ("merchant_pending", 4127),
        ("merchant_cash", -4127),
        ("merchant_pending", 123),
        ("provider_fees_expense", -123),
    ]
    assert ledger_sum(postings, USD) == Money(0, USD)
```

The six rows of the 42.50 capture with a 1.23 fee equal the hand calculation in order, and the ledger sum is zero.

```python
def test_an_entry_on_one_account_or_of_zero_cannot_be_built():
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(Leg.CAPTURE_NET, Account.MERCHANT_CASH, Account.MERCHANT_CASH, Money(1, USD))
    with pytest.raises(UnbalancedEntryError):
        JournalEntry(Leg.CAPTURE_NET, Account.MERCHANT_PENDING, Account.MERCHANT_CASH, Money(0, USD))
```

An entry with the same account on both sides, and an entry of zero, both raise `UnbalancedEntryError`.

```python
def test_a_fee_outside_zero_to_the_amount_is_refused():
    with pytest.raises(UnbalancedEntryError):
        capture_entries(Money(100, USD), Money(101, USD))
    with pytest.raises(UnbalancedEntryError):
        capture_entries(Money(100, USD), Money(-1, USD))
```

A fee above the amount (101 on 100) or below zero is refused.

```python
def test_legs_that_would_move_nothing_are_left_out():
    assert [e.leg for e in capture_entries(Money(1, USD), Money(0, USD))] == [Leg.CAPTURE_GROSS, Leg.CAPTURE_NET]
    assert [e.leg for e in capture_entries(Money(100, USD), Money(100, USD))] == [Leg.CAPTURE_GROSS, Leg.CAPTURE_FEE]
```

A fee of 0 leaves out the fee leg (gross and net remain); a fee equal to the amount leaves out the net leg (gross and fee remain).

```python
def test_a_reversal_undoes_every_leg_of_the_capture_and_both_stay_in_the_ledger():
    captured = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    reversed_ = entries_postings(reversal_entries(Money(4250, USD), Money(123, USD)))
    assert len(reversed_) == 6 and ledger_sum(reversed_, USD) == Money(0, USD)
    assert all(value == Money(0, USD) for value in balances(captured + reversed_, USD).values())
```

The reversal has six rows summing to zero, and after capture plus reversal every account balance is zero, while both sets of rows stay in the list (`captured + reversed_` joins two lists; `reversed_` has a trailing underscore because `reversed` is a Python built-in).

```python
def test_a_reversal_uses_reversal_legs():
    legs = [entry.leg for entry in reversal_entries(Money(4250, USD), Money(123, USD))]
    assert legs == [Leg.REVERSAL_GROSS, Leg.REVERSAL_NET, Leg.REVERSAL_FEE]
```

The legs of a reversal are the three reversal legs, in order.

```python
def test_a_higher_actual_fee_takes_the_difference_out_of_cash_into_the_fee_account():
    entry = fee_correction_entry(Money(123, USD), Money(141, USD))
    assert entry is not None and entry.amount == Money(18, USD)
    captured = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    after = balances(captured + list(entry.postings()), USD)
    assert after[Account.MERCHANT_CASH] == Money(4127 - 18, USD)
    assert after[Account.PROVIDER_FEES_EXPENSE] == Money(141, USD)
```

A recorded fee of 1.23 against an actual 1.41 gives an entry of 0.18. After it the merchant cash is 41.27 minus 0.18 and the fee account balance is 1.41.

```python
def test_a_lower_actual_fee_puts_the_difference_back():
    entry = fee_correction_entry(Money(141, USD), Money(123, USD))
    assert entry is not None and (entry.debit, entry.credit) == (Account.PROVIDER_FEES_EXPENSE, Account.MERCHANT_CASH)
```

A lower actual fee reverses the direction: debit the fee account, credit cash.

```python
def test_equal_fees_need_no_correction():
    assert fee_correction_entry(Money(123, USD), Money(123, USD)) is None
```

Equal fees give `None`.

```python
def test_a_refund_returns_money_and_keeps_the_fee():
    captured = entries_postings(capture_entries(Money(4250, USD), Money(123, USD)))
    refunded = entries_postings(refund_entries(Money(1000, USD)))
    result = balances(captured + refunded, USD)
    assert ledger_sum(refunded, USD) == Money(0, USD)
    assert result[Account.MERCHANT_CASH] == Money(3127, USD) and result[Account.PROVIDER_FEES_EXPENSE] == Money(
        123, USD
    )
```

After a 10.00 refund the cash balance is 31.27 and the fee account still holds 1.23.

### `tests/unit/domain/test_settlement.py` (73 lines)

```python
from datetime import UTC, date, datetime

import pytest

from payments_reconciliation.domain.errors import SettlementFormatError
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind, SettlementRow, format_csv, parse_csv

DAY = date(2026, 10, 20)
STAMP = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)
HEADER = "provider_txn_id,amount,fee,status,settled_at\n"
```

Tests of the settlement file format. `DAY` and `STAMP` are fixed; `HEADER` is the header line with a newline; the helper `row(...)` builds a `SettlementRow` with defaults.

```python
def row(txn="ptx_0001", kind=SettlementKind.SETTLED, amount=4250, fee=123):
    return SettlementRow(txn, kind, Money(amount, "USD"), Money(fee, "USD"), STAMP)
```

The helper: a settled row for `ptx_0001` of 42.50 with a 1.23 fee unless told otherwise.

```python
def test_the_file_round_trips_in_the_providers_shape():
    rows = [row(), row("ptx_0002", amount=1800, fee=52), row("ptx_0002", SettlementKind.REFUNDED, -500, 0)]
    text = format_csv(rows)
    assert text.splitlines()[:2] == [
        "provider_txn_id,amount,fee,status,settled_at",
        "ptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z",
    ]
    assert "ptx_0002,-5.00,0.00,refunded,2026-10-20T23:30:00Z" in text
    assert parse_csv(text, DAY, "USD").rows == tuple(rows)
```

Writing three rows and reading them back gives the same rows (`tuple(rows)`); the first lines are exactly the header and `ptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z`; a refund row is written with a negative amount.

```python
def test_totals_split_settled_money_from_refunds():
    report = parse_csv(
        format_csv([row(), row("ptx_0002", amount=1000), row("ptx_0002", SettlementKind.REFUNDED, -300, 0)]), DAY, "USD"
    )
    assert report.settled_total == Money(5250, "USD") and report.refunded_total == Money(-300, "USD")
    assert len(report.of_kind(SettlementKind.REFUNDED)) == 1
```

The settled total is 52.50, the refunded total is -3.00 and one row is of kind refunded.

```python
@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "empty"),
        ("id,amount\n", "header"),
        (HEADER + "ptx_0001,42.50,1.23,settled\n", "columns"),
        (HEADER + "ptx_0001,abc,1.23,settled,2026-10-20T23:30:00Z\n", "amount"),
        (HEADER + "ptx_0001,42.505,1.23,settled,2026-10-20T23:30:00Z\n", "decimals"),
        (HEADER + "order-9,42.50,1.23,settled,2026-10-20T23:30:00Z\n", "id shape"),
        (HEADER + "ptx_0001,42.50,1.23,pending,2026-10-20T23:30:00Z\n", "status"),
        (HEADER + "ptx_0001,42.50,1.23,settled,yesterday\n", "timestamp"),
        (HEADER + "ptx_0001,-42.50,1.23,settled,2026-10-20T23:30:00Z\n", "negative settled"),
        (HEADER + "ptx_0001,42.50,0.00,refunded,2026-10-20T23:30:00Z\n", "positive refund"),
        (HEADER + "ptx_0001,42.50,-1.00,settled,2026-10-20T23:30:00Z\n", "negative fee"),
    ],
)
```

Eleven malformed files, each labeled by `why`: an empty file, a wrong header, too few columns, a non-numeric amount, three decimals, an id of the wrong shape, an unknown status, a bad timestamp, a negative settled amount, a positive refund and a negative fee.

```python
def test_a_malformed_file_is_refused_and_nothing_is_skipped(text, why):
    with pytest.raises(SettlementFormatError):
        parse_csv(text, DAY, "USD")
```

Each raises `SettlementFormatError`.

```python
def test_the_error_names_the_line():
    with pytest.raises(SettlementFormatError, match="line 3"):
        parse_csv(
            HEADER + "ptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z\nptx_0002,x,0.00,settled,2026-10-20T23:30:00Z\n",
            DAY,
            "USD",
        )
```

A bad amount on the third line gives an error message containing `line 3` (`match=` is a regular expression).

```python
def test_blank_lines_are_ignored():
    assert len(parse_csv(HEADER + "\nptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z\n\n", DAY, "USD").rows) == 1
```

Blank lines before and after a row do not matter.

```python
def test_a_row_needs_a_timezone():
    with pytest.raises(SettlementFormatError):
        SettlementRow("ptx_0001", SettlementKind.SETTLED, Money(1, "USD"), Money(0, "USD"), datetime(2026, 10, 20))
```

A row whose time has no zone is refused.

### `tests/unit/domain/test_reconciliation.py` (187 lines)

```python
"""Matching on the stable id and classifying each difference by its own cause."""

from datetime import UTC, date, datetime, time
from uuid import UUID

import pytest

from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState
from payments_reconciliation.domain.reconciliation import Cause, Handling, LedgerView, cutoff_instant, reconcile
from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, SettlementRow

DAY = date(2026, 10, 20)
CUTOFF = time(23, 0)
USD = "USD"
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
LATE = datetime(2026, 10, 20, 23, 40, tzinfo=UTC)
STAMP = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)
```

Matching on the stable id and classifying each difference by its own cause. Constants: the business day, the 23:00 cutoff, a noon capture time, a late capture time (23:40) and the settlement stamp.

```python
def payment(txn, amount=4250, fee=123, at=NOON, state=PaymentState.CAPTURED, serial=1):
    return LedgerPayment(
        UUID(int=serial), txn, Money(amount, USD), Money(fee, USD), state, PaymentSource.SERVICE, at, Money(0, USD)
    )
```

A helper that builds a `LedgerPayment` with defaults (4250 amount, 123 fee, captured at noon, state `CAPTURED`); `UUID(int=serial)` makes an id from a small number.

```python
def settled(txn, amount=4250, fee=123):
    return SettlementRow(txn, SettlementKind.SETTLED, Money(amount, USD), Money(fee, USD), STAMP)
```

A helper for a settled report row.

```python
def refunded(txn, amount):
    return SettlementRow(txn, SettlementKind.REFUNDED, Money(-amount, USD), Money(0, USD), STAMP)
```

A helper for a refunded row (the amount is negated).

```python
def run(payments, rows, refunds=None, carried=None, max_carry_days=2):
    report = SettlementReport(DAY, USD, tuple(rows))
    ledger = LedgerView(tuple(payments), refunds or {})
    return reconcile(ledger, report, carried_days=carried or {}, cutoff=CUTOFF, max_carry_days=max_carry_days)
```

Runs `reconcile` with a report made from the rows, a ledger view made from the payments and refunds, and the cutoff and carry settings.

```python
def causes(result):
    return {item.provider_txn_id: item.cause for item in result.breaks}
```

A dictionary from transaction id to cause for the result's breaks, to compare in one assertion.

```python
def test_a_clean_day_has_no_breaks_and_every_transaction_matched():
    result = run([payment("ptx_0001"), payment("ptx_0002", serial=2)], [settled("ptx_0001"), settled("ptx_0002")])
    assert result.breaks == () and [m.provider_txn_id for m in result.matched] == ["ptx_0001", "ptx_0002"]
```

Two agreeing transactions: no breaks, both matched.

```python
def test_the_join_is_on_the_provider_id_not_on_amount_and_time():
    # Same amount, same minute, different ids: a fuzzy join would pair them; the stable id does not.
    result = run([payment("ptx_0001")], [settled("ptx_0002")])
    assert causes(result) == {"ptx_0001": Cause.LEDGER_ONLY_UNEXPLAINED, "ptx_0002": Cause.MISSING_CAPTURE}
```

A payment `ptx_0001` and a report row `ptx_0002` with identical amounts do not pair: one is ledger-only, the other is a missing capture. A fuzzy join on amount and time would have paired them.

```python
def test_the_order_of_the_rows_does_not_matter():
    rows = [settled("ptx_0003"), settled("ptx_0001"), settled("ptx_0002")]
    payments = [payment("ptx_0002", serial=2), payment("ptx_0003", serial=3), payment("ptx_0001")]
    assert run(payments, rows).breaks == ()
```

Shuffled input gives no breaks.

```python
def test_a_capture_after_the_cutoff_that_is_not_in_the_report_is_a_timing_difference():
    result = run([payment("ptx_0050", at=LATE)], [])
    assert causes(result) == {"ptx_0050": Cause.TIMING}
    assert result.breaks[0].handling is Handling.CARRY
```

A 23:40 capture missing from the report is `TIMING`, handled by carrying.

```python
def test_a_capture_before_the_cutoff_that_is_not_in_the_report_is_not_explained():
    result = run([payment("ptx_0051")], [])
    assert causes(result) == {"ptx_0051": Cause.LEDGER_ONLY_UNEXPLAINED}
    assert result.breaks[0].handling is Handling.HUMAN
```

A noon capture missing from the report is `LEDGER_ONLY_UNEXPLAINED`, handled by a human.

```python
def test_the_fee_difference_is_classified_apart_from_the_amount_difference():
    result = run([payment("ptx_0112")], [settled("ptx_0112", fee=141)])
    assert causes(result) == {"ptx_0112": Cause.FEE_DISCREPANCY}
    assert result.breaks[0].detail == "fee recorded 1.23, settled 1.41"
```

Fees 1.23 against 1.41 give `FEE_DISCREPANCY` and the detail text `fee recorded 1.23, settled 1.41`.

```python
def test_a_different_amount_is_a_real_break_even_when_the_fee_differs_too():
    result = run([payment("ptx_0400", amount=9500)], [settled("ptx_0400", amount=9999, fee=290)])
    assert causes(result) == {"ptx_0400": Cause.AMOUNT_MISMATCH}
    assert result.breaks[0].handling is Handling.HUMAN
```

Different amounts (and fees) give `AMOUNT_MISMATCH`, handled by a human.

```python
def test_settled_at_the_provider_with_nothing_in_the_ledger_is_a_missing_capture():
    result = run([], [settled("ptx_0200", amount=6000, fee=174)])
    assert causes(result) == {"ptx_0200": Cause.MISSING_CAPTURE}
    assert (result.breaks[0].settlement_amount, result.breaks[0].settlement_fee) == (Money(6000, USD), Money(174, USD))
```

An empty ledger and a settled row give `MISSING_CAPTURE`, and the break carries the report's amount and fee.

```python
def test_two_active_payments_for_one_provider_id_are_a_duplicate_on_our_side():
    result = run([payment("ptx_0215", serial=1), payment("ptx_0215", serial=2)], [settled("ptx_0215")])
    assert causes(result) == {"ptx_0215": Cause.DUPLICATE_ON_OUR_SIDE}
```

Two payments with one id give `DUPLICATE_ON_OUR_SIDE`.

```python
def test_a_reversed_duplicate_no_longer_counts():
    payments = [payment("ptx_0215", serial=1), payment("ptx_0215", serial=2, state=PaymentState.DUPLICATE_REVERSED)]
    assert run(payments, [settled("ptx_0215")]).breaks == ()
```

If the second payment is `DUPLICATE_REVERSED`, there is no break.

```python
def test_a_refund_only_the_provider_has_is_a_provider_only_refund():
    result = run([payment("ptx_0300")], [settled("ptx_0300"), refunded("ptx_0300", 4250)])
    assert causes(result) == {"ptx_0300": Cause.PROVIDER_ONLY_REFUND}
    assert result.breaks[0].kind is SettlementKind.REFUNDED and result.breaks[0].settlement_amount == Money(4250, USD)
    assert [m.kind for m in result.matched] == [SettlementKind.SETTLED]
```

A refund row with no ledger refund gives `PROVIDER_ONLY_REFUND`, of kind refunded with the refund amount; the settled part still matches.

```python
def test_a_refund_in_both_that_agrees_matches():
    result = run(
        [payment("ptx_0300")], [settled("ptx_0300"), refunded("ptx_0300", 1000)], refunds={"ptx_0300": Money(1000, USD)}
    )
    assert result.breaks == ()
```

Equal refunds on both sides give no breaks.

```python
def test_a_refund_in_the_ledger_only_or_for_another_amount_needs_a_person():
    only_ledger = run([payment("ptx_0300")], [settled("ptx_0300")], refunds={"ptx_0300": Money(1000, USD)})
    assert causes(only_ledger) == {"ptx_0300": Cause.REFUND_ONLY_IN_LEDGER}
    different = run(
        [payment("ptx_0300")], [settled("ptx_0300"), refunded("ptx_0300", 900)], refunds={"ptx_0300": Money(1000, USD)}
    )
    assert causes(different) == {"ptx_0300": Cause.REFUND_AMOUNT_MISMATCH}
    assert different.breaks[0].handling is Handling.HUMAN
```

A refund only in the ledger gives `REFUND_ONLY_IN_LEDGER`; refunds of 9.00 against 10.00 give `REFUND_AMOUNT_MISMATCH`, handled by a human.

```python
def test_two_settled_rows_for_one_id_is_a_provider_duplicate():
    result = run([payment("ptx_0001")], [settled("ptx_0001"), settled("ptx_0001")])
    assert causes(result) == {"ptx_0001": Cause.PROVIDER_DUPLICATE}
```

Two settled rows for one id give `PROVIDER_DUPLICATE`.

```python
def test_a_carried_difference_that_is_still_not_in_the_report_keeps_waiting():
    result = run([payment("ptx_0050", at=datetime(2026, 10, 19, 23, 40, tzinfo=UTC))], [], carried={"ptx_0050": 1})
    assert causes(result) == {"ptx_0050": Cause.TIMING}
```

A carried transaction (1 day) still missing is `TIMING` again.

```python
def test_a_carried_difference_that_waited_too_long_goes_to_a_person():
    result = run([payment("ptx_0050", at=datetime(2026, 10, 18, 23, 40, tzinfo=UTC))], [], carried={"ptx_0050": 2})
    assert causes(result) == {"ptx_0050": Cause.TIMING_OVERDUE}
    assert result.breaks[0].handling is Handling.HUMAN
```

At 2 days with a limit of 2 it becomes `TIMING_OVERDUE`, handled by a human.

```python
def test_a_carried_difference_that_arrives_matches_with_no_break():
    result = run(
        [payment("ptx_0050", at=datetime(2026, 10, 19, 23, 40, tzinfo=UTC))],
        [settled("ptx_0050")],
        carried={"ptx_0050": 1},
    )
    assert result.breaks == () and [m.provider_txn_id for m in result.matched] == ["ptx_0050"]
```

When the row finally appears, there is no break and the transaction is matched.

```python
def test_every_cause_has_a_handling():
    from payments_reconciliation.domain.reconciliation import HANDLING

    assert set(HANDLING) == set(Cause)
```

The `HANDLING` table has an entry for every `Cause` (set equality).

```python
@pytest.mark.parametrize(
    ("cause", "handling"),
    [
        (Cause.TIMING, Handling.CARRY),
        (Cause.FEE_DISCREPANCY, Handling.AUTOMATIC),
        (Cause.MISSING_CAPTURE, Handling.FROM_REPORT),
        (Cause.DUPLICATE_ON_OUR_SIDE, Handling.FROM_REPORT),
        (Cause.PROVIDER_ONLY_REFUND, Handling.FROM_REPORT),
        (Cause.AMOUNT_MISMATCH, Handling.HUMAN),
    ],
)
```

The lesson's six causes with the handling the lesson says.

```python
def test_the_lessons_six_causes_are_handled_as_the_lesson_says(cause, handling):
    from payments_reconciliation.domain.reconciliation import HANDLING

    assert HANDLING[cause] is handling
```

Looks each cause up in `HANDLING`; `is` compares enum members.

```python
def test_the_cutoff_instant_is_the_batch_close_in_utc():
    assert cutoff_instant(DAY, CUTOFF) == datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
```

The cutoff instant is 23:00 UTC of the day.

```python
def test_a_payment_can_carry_a_refund_up_to_its_amount_only():
    from payments_reconciliation.domain.errors import ReconciliationError

    base = payment("ptx_0001")
    assert base.with_refund(Money(1000, USD)).refunded == Money(1000, USD)
    with pytest.raises(ReconciliationError):
        base.with_refund(Money(4251, USD))
    with pytest.raises(ReconciliationError):
        base.with_refund(Money(0, USD))
    assert base.reversed_as_duplicate().active is False and base.with_fee(Money(141, USD)).fee == Money(141, USD)
```

`with_refund` accepts 10.00; refuses 42.51 and 0; and the other helpers `reversed_as_duplicate` and `with_fee` give the expected copies.

### `tests/unit/application/test_lesson_scenario.py` (303 lines)

```python
"""The lesson's steps 1 to 6, on the in-memory ledger: the same code path as the real run, without a database."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from payments_reconciliation.application.daily_report import render
from payments_reconciliation.application.ports import BreakStatus
from payments_reconciliation.application.resolve_breaks import Outcome
from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import PaymentSource, PaymentState
from payments_reconciliation.domain.reconciliation import Cause
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants
from tests.unit.conftest import World, build_world

# The lesson plants its six breaks at positions 50, 112, 200, 215, 300 and 400 of 500; most tests here use the same
# six causes at positions a 60-payment day reaches, and one test runs the lesson's exact day.
```

The lesson's steps 1 to 6 on the in-memory ledger: the same code path as the real run, without a database. `dataclasses` is used to copy objects with a field changed.

```python
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)
PLANTED = date(2026, 10, 20)
NEXT = date(2026, 10, 21)
```

The comment explains the trick: the lesson plants its six breaks at positions 50, 112, 200, 215, 300 and 400 of 500; most tests use the same six causes at positions a 60-payment day reaches (5, 8, 12, 15, 20, 25), and one test runs the lesson's exact day. `PLANTED` and `NEXT` are the two days.

```python
LESSON_SIX = {
    "ptx_0050": Cause.TIMING,
    "ptx_0112": Cause.FEE_DISCREPANCY,
    "ptx_0200": Cause.MISSING_CAPTURE,
    "ptx_0215": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0300": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0400": Cause.AMOUNT_MISMATCH,
}
SIX = {
    "ptx_0005": Cause.TIMING,
    "ptx_0008": Cause.FEE_DISCREPANCY,
    "ptx_0012": Cause.MISSING_CAPTURE,
    "ptx_0015": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0020": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0025": Cause.AMOUNT_MISMATCH,
}
```

The expected cause of each of the lesson's six ids, and `SIX`, the same for the 60-payment day.

```python
@pytest.fixture(scope="module")
def planted_files(tmp_path_factory):
    """The planted day is simulated once; each test copies the resulting state into its own world."""
    return tmp_path_factory.mktemp("planted")
```

A module-scoped fixture returning a temporary folder for the planted day.

```python
def planted_world(directory) -> World:
    world = build_world(directory)
    world.services.simulator.run(DayPlan(PLANTED, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    return world
```

Builds a world and simulates the planted 60-payment day in it.

```python
def test_a_clean_day_reconciles_with_zero_breaks_and_every_transaction_matched_on_its_id(world):
    result = world.services.simulator.run(DayPlan(date(2026, 10, 18), 200, 42, "ptx_a"))
    outcome = world.services.reconcile.execute(date(2026, 10, 18))
    assert result.recorded == 200 and outcome.breaks == []
    assert outcome.run.clean_transactions == 200 and outcome.run.break_count == 0
    assert world.db.ledger_sum() == 0
```

200 payments, nothing planted: 200 recorded, no breaks, 200 clean, and the ledger sums to zero.

```python
def test_the_same_seed_produces_the_same_day(tmp_path):
    first, second = build_world(tmp_path / "a"), build_world(tmp_path / "b")
    for world in (first, second):
        world.services.simulator.run(DayPlan(PLANTED, 120, 7, "ptx_", plant_breaks=False))
    assert first.services.files.read(PLANTED) == second.services.files.read(PLANTED)
```

Two worlds with the same seed write byte-identical settlement files (`tmp_path / "a"` joins a path with a name).

```python
def test_the_planted_day_finds_all_six_breaks_each_with_its_own_cause(tmp_path):
    world = planted_world(tmp_path)
    outcome = world.services.reconcile.execute(PLANTED)
    found = {stored.finding.provider_txn_id: stored.finding.cause for stored in outcome.breaks}
    assert found == SIX
    assert outcome.run.break_count == 6 and outcome.run.clean_transactions == 54
```

Reconciling the planted day finds exactly the six, with 54 clean transactions.

```python
def test_the_lessons_exact_day_of_500_payments_has_the_six_breaks_at_the_lessons_ids(tmp_path):
    world = build_world(tmp_path)
    world.services.simulator.run(DayPlan(PLANTED, 500, 43, "ptx_", plant_breaks=True))
    outcome = world.services.reconcile.execute(PLANTED)
    assert {b.finding.provider_txn_id: b.finding.cause for b in outcome.breaks} == LESSON_SIX
    assert outcome.run.clean_transactions == 494 and outcome.run.settlement_rows == 500
```

The lesson's real day: 500 payments, the six breaks at the lesson's ids, 494 clean and 500 settlement rows.

```python
def test_the_breaks_are_planted_by_cause_not_by_editing_files(tmp_path):
    world = build_world(tmp_path)
    result = world.services.simulator.run(DayPlan(PLANTED, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    assert result.crashed_after_provider == ["ptx_0012"]
    assert result.duplicated == ["ptx_0015"]
    assert result.refunded_at_provider_only == ["ptx_0020"]
    assert world.db.payments_of("ptx_0012") == [] and len(world.db.payments_of("ptx_0015")) == 2
    assert world.services.provider.executed["capture"] == 60  # the crashed one was captured at the provider
```

The simulator reports a crash after the provider at `ptx_0012`, a duplicate at `ptx_0015` and a provider-only refund at `ptx_0020`; the ledger really has no payment for the first and two for the second; the provider captured 60 times.

```python
def test_resolving_fixes_the_four_breaks_that_can_be_fixed_and_leaves_the_amount_for_a_person(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    resolutions = {item.stored.finding.provider_txn_id: item for item in world.services.resolve.execute(PLANTED)}
    assert {k: v.outcome for k, v in resolutions.items()} == {
        "ptx_0005": Outcome.CARRIED,
        "ptx_0008": Outcome.RESOLVED,
        "ptx_0012": Outcome.RESOLVED,
        "ptx_0015": Outcome.RESOLVED,
        "ptx_0020": Outcome.RESOLVED,
        "ptx_0025": Outcome.NEEDS_REVIEW,
    }
    assert resolutions["ptx_0008"].message == "fee corrected, +0.18 posted, ledger balanced"
    status = {b.finding.provider_txn_id: b.status for b in world.db.breaks.values()}
    assert status == {
        "ptx_0005": BreakStatus.CARRIED,
        "ptx_0008": BreakStatus.AUTO_RESOLVED,
        "ptx_0012": BreakStatus.RESOLVED,
        "ptx_0015": BreakStatus.RESOLVED,
        "ptx_0020": BreakStatus.RESOLVED,
        "ptx_0025": BreakStatus.NEEDS_REVIEW,
    }
    assert world.db.ledger_sum() == 0
```

The outcome of each break (carried, resolved x4, needs review), the fee message `fee corrected, +0.18 posted, ledger balanced`, the final status of each break, and a zero ledger sum.

```python
def test_the_fee_is_corrected_with_one_balanced_entry_and_the_recorded_fee_follows_the_provider(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    payment = world.db.payments_of("ptx_0008")[0]
    assert payment.fee == Money(141, "USD")
    cash = -sum(amount for account, amount in world.db.postings_of(payment.id) if account is Account.MERCHANT_CASH)
    assert cash == 4250 - 141  # what the provider really paid out
    corrections = [row for row in world.db.postings if row[1] == "FEE_CORRECTION"]
    assert [(row[4], row[5].minor) for row in corrections] == [
        (Account.MERCHANT_CASH, 18),
        (Account.PROVIDER_FEES_EXPENSE, -18),
    ]
```

After resolving, the payment's fee is 1.41, the cash credited to the merchant equals the amount minus 1.41, and the correction postings are +18 on cash and -18 on the fee account.

```python
def test_a_missing_capture_is_inserted_from_the_report_and_the_provider_is_never_called_again(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    captured_at_provider = world.services.provider.executed["capture"]
    world.services.resolve.execute(PLANTED)
    [inserted] = world.db.payments_of("ptx_0012")
    assert (inserted.source, inserted.amount) == (PaymentSource.RECONCILIATION, Money(6000, "USD"))
    assert len(world.db.postings_of(inserted.id)) == 6
    assert world.services.provider.executed["capture"] == captured_at_provider  # money moved once, at the provider
    assert world.services.provider.executed["refund"] == 1
```

The inserted payment has the source `RECONCILIATION`, the report's amount and six rows; the provider's capture count did not change.

```python
def test_a_duplicate_is_reversed_not_deleted_and_exactly_one_capture_stays(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    first, second = world.db.payments_of("ptx_0015")
    assert (first.state, second.state) == (PaymentState.CAPTURED, PaymentState.DUPLICATE_REVERSED)
    assert len(world.db.postings_of(second.id)) == 12  # the capture's six rows and their six reversals stay on record
    assert sum(amount for _, amount in world.db.postings_of(second.id)) == 0
```

The first payment stays `CAPTURED`, the second becomes `DUPLICATE_REVERSED`; its twelve rows (six plus six reversals) stay on record and sum to zero.

```python
def test_a_refund_only_the_provider_had_is_posted_from_the_report(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    [payment] = world.db.payments_of("ptx_0020")
    assert payment.refunded == Money(2500, "USD") and len(world.db.postings_of(payment.id)) == 10
    assert world.db.refunds[0].reference == "settlement-refund:ptx_0020"
```

The payment shows 25.00 refunded and ten rows; the refund reference is `settlement-refund:ptx_0020`.

```python
def test_running_every_resolver_again_changes_nothing(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    with world.db.unit_of_work() as uow:
        before = (uow.ledger.digest(), len(world.db.postings), len(world.db.payments), len(world.db.refunds))
    second = world.services.resolve.execute(PLANTED)
    with world.db.unit_of_work() as uow:
        after = (uow.ledger.digest(), len(world.db.postings), len(world.db.payments), len(world.db.refunds))
    assert before == after
    outcomes = {item.stored.finding.provider_txn_id: item.outcome for item in second}
    assert (
        outcomes["ptx_0008"]
        is outcomes["ptx_0012"]
        is outcomes["ptx_0015"]
        is outcomes["ptx_0020"]
        is Outcome.ALREADY_RESOLVED
    )
```

The ledger digest and the counts of postings, payments and refunds are identical before and after a second resolve; the four fixable breaks report `ALREADY_RESOLVED` (a chain of `is` comparisons is true when all are the same member).

```python
def test_resolvers_are_idempotent_even_without_the_break_record(tmp_path):
    """The ledger itself says "already resolved": reopen the break rows and run again."""
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    for stored in list(world.db.breaks.values()):
        if stored.status in {BreakStatus.RESOLVED, BreakStatus.AUTO_RESOLVED}:
            world.db.breaks[stored.id] = dataclasses.replace(stored, status=BreakStatus.OPEN)
    before = len(world.db.postings)
    outcomes = [item.outcome for item in world.services.resolve.execute(PLANTED)]
    assert Outcome.RESOLVED not in outcomes and len(world.db.postings) == before
```

The docstring: the ledger itself says "already resolved". The test reopens the resolved break rows, resolves again, and requires that nothing is resolved twice and no posting is added.

```python
def test_reconciling_a_day_again_after_the_fixes_leaves_only_the_real_break(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    again = world.services.reconcile.execute(PLANTED)
    assert {b.finding.provider_txn_id for b in again.breaks if b.status is BreakStatus.NEEDS_REVIEW} == {"ptx_0025"}
    assert len(world.db.breaks) == 6  # the same rows, not new ones
    assert [b.finding.provider_txn_id for b in world.services.worklist.execute()] == ["ptx_0025"]
```

Reconciling again leaves only `ptx_0025` needing review, still six break rows (the same rows), and the worklist holds that one.

```python
def test_the_daily_report_balances_the_settled_total_and_the_worklist_holds_only_the_amount_mismatch(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    report = world.services.report.execute(PLANTED)
    assert report.balances and report.settled_rows == 59
    assert [line.provider_txn_id for line in report.worklist] == ["ptx_0025"]
    assert [line.provider_txn_id for line in report.auto_resolved] == ["ptx_0008"]
    assert sorted(line.provider_txn_id for line in report.resolved) == ["ptx_0012", "ptx_0015", "ptx_0020"]
    assert [line.provider_txn_id for line in report.carried] == ["ptx_0005"]
    assert report.buckets["worklist"] == Money(9999, "USD")
    assert report.ledger_difference == Money(499, "USD")  # 99.99 at the provider against 95.00 in the ledger
    assert report.ledger_sum == Money(0, "USD") and report.clean_transactions == 54
```

The report balances with 59 settled rows; the worklist, auto-resolved, resolved and carried sections hold the expected ids; the worklist bucket is 99.99, the difference against the ledger is 4.99 (99.99 at the provider against 95.00 in the ledger), the ledger sum is 0 and 54 transactions were clean.

```python
def test_the_timing_difference_clears_itself_on_the_day_it_settles(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    world.services.simulator.run(DayPlan(NEXT, 30, 44, "ptx_n"))
    outcome = world.services.reconcile.execute(NEXT)
    assert [stored.finding.provider_txn_id for stored in outcome.cleared_carried] == ["ptx_0005"]
    assert outcome.breaks == []
    [timing] = world.db.break_of("ptx_0005")
    assert timing.status is BreakStatus.CLEARED and timing.cleared_on == NEXT
    report = world.services.report.execute(NEXT)
    assert [line.provider_txn_id for line in report.cleared_carried] == ["ptx_0005"] and report.balances
```

After a next-day simulation the carried break `ptx_0005` is reported as cleared, the day has no breaks, the stored break is `CLEARED` with the clearing date, and the next day's report balances.

```python
def test_rerunning_the_planted_day_after_its_timing_difference_settled_stays_quiet(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    world.services.simulator.run(DayPlan(NEXT, 30, 44, "ptx_n"))
    world.services.reconcile.execute(NEXT)
    again = world.services.reconcile.execute(PLANTED)
    assert again.run.break_count == 1 and again.run.clean_transactions == 58
    assert [b.finding.provider_txn_id for b in again.breaks if b.status is BreakStatus.NEEDS_REVIEW] == ["ptx_0025"]
    text = render(world.services.report.execute(PLANTED))
    assert "Carried and settled later: ptx_0005" in text
```

Re-running the old day gives one break and 58 clean, and the report text contains `Carried and settled later: ptx_0005`.

```python
def test_a_timing_difference_that_never_settles_escalates_to_the_worklist(tmp_path):
    world = build_world(tmp_path, max_carry_days=1)
    world.services.simulator.run(
        DayPlan(PLANTED, 60, 5, "ptx_", plant_breaks=True, plants=PLANTS)
    )  # ptx_0005 is late; no ptx_0008+
    world.services.reconcile.execute(PLANTED)
    provider = world.services.provider
    provider._captures.pop("ptx_0005")  # the provider loses it: it will never appear in a report
    for day, prefix in ((NEXT, "ptx_n"), (date(2026, 10, 22), "ptx_m")):
        world.services.simulator.run(DayPlan(day, 5, 6, prefix))
        world.services.reconcile.execute(day)
    causes = {b.finding.cause for b in world.db.break_of("ptx_0005")}
    assert Cause.TIMING_OVERDUE in causes
    overdue = next(b for b in world.db.break_of("ptx_0005") if b.finding.cause is Cause.TIMING_OVERDUE)
    assert overdue.status is BreakStatus.NEEDS_REVIEW
    assert any(b.status is BreakStatus.ESCALATED for b in world.db.break_of("ptx_0005"))
```

With a carry limit of 1 the provider "loses" the transaction (its record is removed). After two more days the break `TIMING_OVERDUE` exists with status `NEEDS_REVIEW` and the old carried break is `ESCALATED`.

```python
def test_a_break_that_comes_back_after_it_was_resolved_is_reopened(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    world.services.resolve.execute(PLANTED)
    payment = world.db.payments_of("ptx_0008")[0]
    world.db.payments[payment.id] = payment.with_fee(Money(123, "USD"))  # someone put the old fee back
    world.services.reconcile.execute(PLANTED)
    [fee_break] = world.db.break_of("ptx_0008", "fee_discrepancy")
    assert fee_break.status is BreakStatus.OPEN and fee_break.note == "reappeared after it was resolved"
```

If someone puts the old fee back, a new reconcile reopens the fee break (`OPEN`) with the note `reappeared after it was resolved`.

```python
def test_a_break_that_no_longer_reproduces_is_cleared(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    payment = world.db.payments_of("ptx_0008")[0]
    world.db.payments[payment.id] = payment.with_fee(Money(141, "USD"))  # fixed by hand, outside the job
    world.services.reconcile.execute(PLANTED)
    [fee_break] = world.db.break_of("ptx_0008", "fee_discrepancy")
    assert fee_break.status is BreakStatus.CLEARED
```

If the fee is fixed by hand before resolving, the next run marks the break `CLEARED`.

```python
def test_a_refund_cannot_be_posted_for_a_payment_the_ledger_does_not_have(tmp_path):
    world = planted_world(tmp_path)
    world.services.reconcile.execute(PLANTED)
    [refund_break] = world.db.break_of("ptx_0020", "provider_only_refund")
    payment = world.db.payments_of("ptx_0020")[0]
    del world.db.payments[payment.id]
    resolution = world.services.resolve.resolve(refund_break.id)
    assert resolution.outcome is Outcome.NEEDS_REVIEW and world.db.refunds == []
```

With the payment deleted from the store, the refund resolver asks for a human and records no refund.

```python
def test_an_unknown_break_id_is_an_error(world):
    with pytest.raises(LookupError):
        world.services.resolve.resolve(999)
```

Resolving a break id that does not exist raises `LookupError`.

### `tests/unit/application/test_report_and_capture.py` (72 lines)

```python
from datetime import UTC, date, datetime

import pytest

from payments_reconciliation.application.daily_report import ReportNotAvailableError, render
from payments_reconciliation.application.ports import CaptureResult
from payments_reconciliation.application.record_capture import RecordCapture
from payments_reconciliation.domain.errors import SettlementMissingError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants, SimulatedCrashError
from payments_reconciliation.infrastructure.stub_provider import StubProvider
from tests.unit.fakes import FixedClock, SequentialIds

DAY = date(2026, 10, 20)
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)
```

Tests of the report and of the capture path. `PLANTS` is the 60-payment setup again.

```python
def test_reconciling_a_day_without_a_file_names_the_missing_file(world):
    with pytest.raises(SettlementMissingError, match="2026-10-20"):
        world.services.reconcile.execute(DAY)
```

Reconciling a day with no file raises `SettlementMissingError` mentioning the date.

```python
def test_the_report_of_a_day_that_was_not_reconciled_is_not_available(world):
    world.services.simulator.run(DayPlan(DAY, 10, 1, "ptx_"))
    with pytest.raises(ReportNotAvailableError):
        world.services.report.execute(DAY)
```

A day that was simulated (so the file exists) but never reconciled raises `ReportNotAvailableError`.

```python
def test_the_report_text_has_the_lessons_sections(tmp_path):
    from tests.unit.conftest import build_world

    world = build_world(tmp_path)
    world.services.simulator.run(DayPlan(DAY, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    world.services.reconcile.execute(DAY)
    world.services.resolve.execute(DAY)
    text = render(world.services.report.execute(DAY))
    assert "Matched at the first pass: 54 transactions" in text
    assert "Auto-resolved: ptx_0008 (fee_discrepancy)" in text
    assert "Resolved from the report: ptx_0012 (missing_capture), ptx_0015 (duplicate_on_our_side)" in text
    assert "ptx_0020 (provider_only_refund)" in text
    assert "Carried to the next day: ptx_0005 (timing)" in text
    assert "Worklist (needs a human): ptx_0025 (amount_mismatch:" in text
    assert "[balances]" in text and "Ledger sum across every posting: 0.00 USD" in text
```

After simulating, reconciling and resolving the planted day, the rendered text contains the lesson's lines: 54 matched, the auto-resolved fee, the three resolved from the report, the provider refund, the carried timing difference, the worklist entry, `[balances]` and the zero ledger sum.

```python
def test_a_capture_is_recorded_once_per_request_key():
    from tests.unit.fakes import InMemoryDatabase

    db = InMemoryDatabase()
    provider = StubProvider(FeePolicy(290))
    clock = FixedClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    capture = RecordCapture(db.unit_of_work, provider, clock, SequentialIds())
    first = capture.execute("cap-1", Money(4250, "USD"))
    second = capture.execute("cap-1", Money(4250, "USD"))
    assert first == second and len(db.payments) == 1 and len(db.postings) == 6
    assert provider.executed["capture"] == 1 and first.provider_txn_id == "ptx_0001" and first.fee == Money(123, "USD")
```

A capture with the same key twice gives equal results, one payment, six postings and one provider capture, with the first provider id `ptx_0001` and fee 1.23.

```python
def test_a_crash_after_the_provider_captured_leaves_money_moved_and_nothing_recorded():
    from tests.unit.fakes import InMemoryDatabase

    def die(result: CaptureResult) -> None:
        raise SimulatedCrashError("killed")

    db = InMemoryDatabase()
    provider = StubProvider(FeePolicy(290))
    capture = RecordCapture(db.unit_of_work, provider, FixedClock(), SequentialIds(), die)
    with pytest.raises(SimulatedCrashError):
        capture.execute("cap-1", Money(4250, "USD"))
    assert db.payments == {} and db.postings == [] and db.keys == {}
    assert provider.executed["capture"] == 1
```

The hook `die` raises after the provider's answer. The capture raises `SimulatedCrashError`, the ledger and key store are empty (the transaction rolled back), and the provider executed once.

### `tests/unit/infrastructure/test_logging_retry.py` (82 lines)

```python
import io
import logging
import random

import pytest

from payments_reconciliation.application.logs import fields
from payments_reconciliation.infrastructure.logging import configure_logging, redact
from payments_reconciliation.infrastructure.retry import retry
```

Tests of log redaction, the logfmt formatter and the retry helper. The imports: `io`, `logging`, `random`, `pytest` and the project parts.

```python
@pytest.mark.parametrize(
    "text", ["4242424242424242", "4242 4242 4242 4242", "card 4242-4242-4242-4242 declined", "order 12345678901234567"]
)
```

Seven bad (name, value) pairs: port 0, port 70000, port `abc`, fee 10001, 11 retry attempts, level `LOUD`, pool size -3.

```python
def test_anything_shaped_like_a_card_number_is_masked(text):
    assert "4242" not in redact(text) and "[redacted-number]" in redact(text)
```

After `redact`, `4242` is gone and `[redacted-number]` appears.

```python
def test_ordinary_numbers_are_left_alone():
    assert redact("amount 4250 id 1234567 ts 2026-10-20T09:00:00") == "amount 4250 id 1234567 ts 2026-10-20T09:00:00"
```

Amounts, a 7-digit id and a timestamp are unchanged.

```python
def test_log_lines_are_logfmt_with_fields_and_withhold_secrets():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("t").info(
        "captured", extra=fields(payment_id="p1", card_token="tok_secret", note="a b", pan="4242424242424242")
    )
    line = stream.getvalue().strip()
    assert line.startswith("ts=") and "level=INFO" in line and "msg=captured" in line
    assert "payment_id=p1" in line and 'note="a b"' in line
    assert "card_token=[withheld]" in line and "tok_secret" not in line
    assert "4242424242424242" not in line
```

Logging into an in-memory stream: the line starts with `ts=`, has `level=INFO` and `msg=captured`, shows `payment_id=p1`, quotes a value containing a space (`note="a b"`), withholds the token (`card_token=[withheld]`, no `tok_secret`) and masks the card number.

```python
def test_exceptions_are_logged_on_one_line():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed")
    assert len(stream.getvalue().strip().splitlines()) == 1 and "ValueError" in stream.getvalue()
```

A logged exception stays on one line and names `ValueError`.

```python
def test_retry_returns_the_first_success_without_sleeping():
    sleeps: list[float] = []
    assert retry(lambda: 7, attempts=3, base_delay_seconds=1, retry_on=(OSError,), sleep=sleeps.append) == 7
    assert sleeps == []
```

A function that works at once returns its value (7) and never sleeps.

```python
def test_retry_backs_off_exponentially_with_jitter_and_stops_at_the_last_attempt():
    calls: list[int] = []
    sleeps: list[float] = []
    notes: list[int] = []

    def flaky():
        calls.append(1)
        raise OSError("down")

    with pytest.raises(OSError, match="down"):
        retry(
            flaky,
            attempts=4,
            base_delay_seconds=1,
            retry_on=(OSError,),
            sleep=sleeps.append,
            rng=random.Random(1),
            on_retry=lambda attempt, error: notes.append(attempt),
        )
    assert len(calls) == 4 and notes == [1, 2, 3]
    assert [0.5 <= pause / (2**index) <= 1.0 for index, pause in enumerate(sleeps)] == [True, True, True]
```

A function that always fails with `OSError`. With 4 attempts it is called 4 times, `on_retry` hears attempts 1, 2, 3, and the three pauses fall between 0.5 and 1.0 times 1, 2 and 4 seconds (exponential growth with jitter). A seeded `random.Random(1)` makes the jitter repeatable. The last error propagates.

```python
def test_retry_does_not_catch_other_errors():
    with pytest.raises(KeyError):
        retry(lambda: {"a": 1}["x"], attempts=3, base_delay_seconds=0, retry_on=(OSError,))
```

A `KeyError` is not in `retry_on`, so it passes straight through.

```python
def test_retry_needs_at_least_one_attempt():
    with pytest.raises(ValueError, match="attempts"):
        retry(lambda: 1, attempts=0, base_delay_seconds=0, retry_on=(OSError,))
```

`attempts=0` raises `ValueError`.

### `tests/unit/infrastructure/test_provider_files_settings.py` (128 lines)

```python
from datetime import UTC, date, datetime, time, timedelta

import pytest

from payments_reconciliation.domain.errors import PaymentNotFoundError, SettlementMissingError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind
from payments_reconciliation.infrastructure.files import DirectorySettlementFiles
from payments_reconciliation.infrastructure.settings import ConfigurationError, Settings
from payments_reconciliation.infrastructure.stub_provider import StubProvider

CUTOFF = time(23, 0)
DAY = date(2026, 10, 20)
USD = "USD"
```

Tests of the stub provider, the settlement files and the settings. `CUTOFF`, `DAY` and `USD` are constants; `datetime`, `time` and `timedelta` build moments.

```python
def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
```

A helper: the moment `hour:minute` UTC on a day (the default is the test day).

```python
def test_capture_is_idempotent_on_its_key_and_ids_are_sequential():
    provider = StubProvider(FeePolicy(290))
    first = provider.capture(idempotency_key="k1", amount=Money(4250, USD), occurred_at=at(9))
    again = provider.capture(idempotency_key="k1", amount=Money(4250, USD), occurred_at=at(9))
    second = provider.capture(idempotency_key="k2", amount=Money(1800, USD), occurred_at=at(10))
    assert first == again and (first.provider_txn_id, second.provider_txn_id) == ("ptx_0001", "ptx_0002")
    assert first.fee == Money(123, USD) and provider.executed["capture"] == 2
```

The same key twice gives equal results; a new key gives the next id; the ids are `ptx_0001` and `ptx_0002`; the fee on 42.50 is 1.23; two captures really ran.

```python
def test_a_new_series_restarts_the_numbering_with_its_prefix():
    provider = StubProvider(FeePolicy(290))
    provider.capture(idempotency_key="k1", amount=Money(100, USD), occurred_at=at(9))
    provider.start_series("ptx_n")
    assert (
        provider.capture(idempotency_key="k2", amount=Money(100, USD), occurred_at=at(9)).provider_txn_id == "ptx_n0001"
    )
```

After `start_series("ptx_n")` the next id is `ptx_n0001`.

```python
def test_the_batch_holds_what_was_captured_since_the_previous_cutoff():
    provider = StubProvider(FeePolicy(290))
    provider.capture(idempotency_key="before", amount=Money(1000, USD), occurred_at=at(12))
    provider.capture(idempotency_key="late", amount=Money(2000, USD), occurred_at=at(23, 40))
    today = provider.settlement_rows(DAY, CUTOFF)
    tomorrow = provider.settlement_rows(DAY + timedelta(days=1), CUTOFF)
    assert [row.provider_txn_id for row in today] == ["ptx_0001"]
    assert [row.provider_txn_id for row in tomorrow] == ["ptx_0002"]
    assert today[0].settled_at == at(23, 30)
```

A noon capture is in today's batch; a 23:40 capture is in tomorrow's; the row's `settled_at` is 23:30 (30 minutes after the close).

```python
def test_adjustments_change_what_is_settled_not_what_was_captured():
    provider = StubProvider(FeePolicy(290))
    result = provider.capture(idempotency_key="k", amount=Money(4250, USD), occurred_at=at(12))
    provider.adjust_fee(result.provider_txn_id, Money(141, USD))
    provider.adjust_amount(result.provider_txn_id, Money(4300, USD))
    [row] = provider.settlement_rows(DAY, CUTOFF)
    assert (row.amount, row.fee) == (Money(4300, USD), Money(141, USD)) and result.fee == Money(123, USD)
```

After `adjust_fee` and `adjust_amount` the settlement row has the adjusted figures while the capture result keeps the quoted fee.

```python
def test_a_refund_shows_as_a_negative_row_with_the_same_transaction_id():
    provider = StubProvider(FeePolicy(290))
    result = provider.capture(idempotency_key="k", amount=Money(2500, USD), occurred_at=at(12))
    first = provider.refund(
        idempotency_key="r", provider_txn_id=result.provider_txn_id, amount=Money(2500, USD), occurred_at=at(13)
    )
    again = provider.refund(
        idempotency_key="r", provider_txn_id=result.provider_txn_id, amount=Money(2500, USD), occurred_at=at(13)
    )
    rows = provider.settlement_rows(DAY, CUTOFF)
    assert first == again and provider.executed["refund"] == 1
    assert [(row.provider_txn_id, row.kind, row.amount.minor) for row in rows] == [
        ("ptx_0001", SettlementKind.REFUNDED, -2500),
        ("ptx_0001", SettlementKind.SETTLED, 2500),
    ]
```

A refund is idempotent on its key (one really ran) and appears as a refunded row of -25.00 with the same id as the settled row; rows are sorted by id then kind.

```python
def test_a_refund_for_an_unknown_transaction_or_over_the_capture_is_refused():
    provider = StubProvider(FeePolicy(290))
    with pytest.raises(PaymentNotFoundError):
        provider.refund(idempotency_key="r", provider_txn_id="ptx_9999", amount=Money(1, USD), occurred_at=at(12))
    result = provider.capture(idempotency_key="k", amount=Money(100, USD), occurred_at=at(12))
    with pytest.raises(PaymentNotFoundError):
        provider.refund(
            idempotency_key="r2", provider_txn_id=result.provider_txn_id, amount=Money(101, USD), occurred_at=at(12)
        )
```

An unknown transaction and a refund larger than the capture both raise `PaymentNotFoundError`.

```python
def test_files_are_written_whole_and_read_back(tmp_path):
    files = DirectorySettlementFiles(tmp_path / "nested")
    files.write(DAY, "a,b\n")
    assert files.read(DAY) == "a,b\n" and files.path_for(DAY).name == "settlement_2026-10-20.csv"
    assert [p.name for p in (tmp_path / "nested").iterdir()] == ["settlement_2026-10-20.csv"]
    files.write(DAY, "c,d\n")
    assert files.read(DAY) == "c,d\n"
```

Writing into a folder that does not exist creates it; the file name is `settlement_2026-10-20.csv`; only the final file is left (no `.part`); writing again replaces the content.

```python
def test_a_missing_file_is_a_named_error(tmp_path):
    with pytest.raises(SettlementMissingError, match="2026-10-20"):
        DirectorySettlementFiles(tmp_path).read(DAY)
```

Reading a missing file raises `SettlementMissingError` naming the date.

```python
def test_defaults_give_a_working_local_configuration():
    settings = Settings.from_env({"POSTGRES_PASSWORD": "not-a-real-secret"})
    assert settings.postgres.port == 54612 and settings.api.port == 8462
    assert settings.reconciliation.cutoff == time(23, 0) and settings.reconciliation.max_carry_days == 2
    assert settings.reconciliation.currency == "USD" and "not-a-real-secret" not in repr(settings)
```

With only the password the database port is 54612, the API port 8462, the cutoff 23:00, the carry limit 2 and the currency `USD`, and the password is not in the settings' printed form.

```python
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POSTGRES_PORT", "0"),
        ("SETTLEMENT_CUTOFF_UTC", "25:61"),
        ("SETTLEMENT_CUTOFF_UTC", "late"),
        ("CURRENCY", "XXX"),
        ("MAX_CARRY_DAYS", "0"),
        ("PROVIDER_FEE_BPS", "10001"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
```

Seven bad (name, value) pairs: port 0, two unusable cutoffs (`25:61`, `late`), an unsupported currency, a zero carry limit, a fee above 10,000 basis points and a bad log level.

```python
def test_a_bad_value_names_the_variable(name, value):
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({"POSTGRES_PASSWORD": "x", name: value})
```

Each raises `ConfigurationError` whose message names the variable.

```python
def test_the_password_has_no_default():
    with pytest.raises(ConfigurationError, match="POSTGRES_PASSWORD"):
        Settings.from_env({})
```

An empty environment raises `ConfigurationError` mentioning `POSTGRES_PASSWORD`.

### `tests/unit/interfaces/test_http_and_cli.py` (158 lines)

```python
"""The HTTP layer through FastAPI's test client and the command-line handlers, over in-memory storage."""

from __future__ import annotations

import argparse
import dataclasses
from datetime import date

import pytest
from fastapi.testclient import TestClient

from payments_reconciliation.application.health import CheckHealth
from payments_reconciliation.domain.errors import InvalidDateError
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants
from payments_reconciliation.interfaces import cli
from payments_reconciliation.interfaces.http.app import create_app
from tests.unit.conftest import World, build_world

DAY = date(2026, 10, 20)
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)
```

The HTTP layer through FastAPI's `TestClient` and the command-line handlers, over in-memory storage. `argparse` makes the argument object the handlers expect; `dataclasses.replace` copies the container with one use case replaced.

```python
class _Down:
    def check(self) -> None:
        raise ConnectionError("database unreachable")
```

A health probe that raises, imitating an unreachable database.

```python
@pytest.fixture
def client(tmp_path):
    world = build_world(tmp_path)
    world.services.simulator.run(DayPlan(DAY, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    with TestClient(create_app(lambda: world.services), raise_server_exceptions=False) as test_client:
        yield test_client, world
```

A fixture yielding a test client and the world with the planted 60-payment day already simulated. `create_app(lambda: world.services)` builds the real FastAPI app around the fake-backed container; `raise_server_exceptions=False` makes the client return a 500 answer instead of raising.

```python
def test_reconcile_over_http_lists_the_six_breaks_with_their_causes(client):
    test_client, _ = client
    body = test_client.post("/v1/days/2026-10-20/reconcile").json()
    assert {item["provider_txn_id"]: item["cause"] for item in body["breaks"]} == {
        "ptx_0005": "timing",
        "ptx_0008": "fee_discrepancy",
        "ptx_0012": "missing_capture",
        "ptx_0015": "duplicate_on_our_side",
        "ptx_0020": "provider_only_refund",
        "ptx_0025": "amount_mismatch",
    }
    assert body["run"]["break_count"] == 6 and body["run"]["clean_transactions"] == 54
```

`POST /v1/days/2026-10-20/reconcile` returns the six breaks with their causes, a break count of 6 and 54 clean transactions.

```python
def test_resolve_report_and_worklist_over_http(client):
    test_client, _ = client
    test_client.post("/v1/days/2026-10-20/reconcile")
    resolved = test_client.post("/v1/days/2026-10-20/resolve").json()["resolutions"]
    assert {item["provider_txn_id"]: item["outcome"] for item in resolved}["ptx_0008"] == "resolved"
    again = test_client.post("/v1/days/2026-10-20/resolve").json()["resolutions"]
    assert {item["provider_txn_id"]: item["outcome"] for item in again}["ptx_0008"] == "already resolved"
    report = test_client.get("/v1/days/2026-10-20/report").json()
    assert report["balances"] is True and [line["provider_txn_id"] for line in report["worklist"]] == ["ptx_0025"]
    assert "Worklist (needs a human): ptx_0025" in report["text"]
    worklist = test_client.get("/v1/worklist").json()["worklist"]
    assert [item["provider_txn_id"] for item in worklist] == ["ptx_0025"]
```

Resolving twice over HTTP: the fee break is `resolved`, then `already resolved`. The report balances and its worklist holds `ptx_0025`; the text contains the worklist line; `/v1/worklist` lists the same.

```python
def test_errors_have_one_shape_and_the_right_status(client):
    test_client, _ = client
    missing = test_client.post("/v1/days/2026-10-25/reconcile")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "settlement_missing"
    not_run = test_client.get("/v1/days/2026-10-20/report")
    assert not_run.status_code == 404 and not_run.json()["error"]["code"] == "report_not_available"
    bad_date = test_client.post("/v1/days/not-a-date/reconcile")
    assert bad_date.status_code == 400 and bad_date.json()["error"]["code"] == "invalid_request"
```

A day without a file is 404 `settlement_missing`; the report of an unreconciled day is 404 `report_not_available`; a malformed date in the path is 400 `invalid_request`.

```python
def test_a_malformed_settlement_file_is_a_422_that_names_the_line(client):
    test_client, world = client
    world.services.files.write(
        date(2026, 10, 22), "provider_txn_id,amount,fee,status,settled_at\nptx_1,x,0,settled,now\n"
    )
    response = test_client.post("/v1/days/2026-10-22/reconcile")
    assert response.status_code == 422 and "line 2" in response.json()["error"]["message"]
```

A file with a bad row gives 422 and a message mentioning `line 2`.

```python
def test_health_live_and_a_down_dependency(client):
    test_client, world = client
    assert test_client.get("/health/live").json() == {"status": "ok"}
    assert test_client.get("/health").json() == {"status": "ok", "checks": {}}
    down = dataclasses.replace(world.services, health=CheckHealth({"database": _Down()}))
    with TestClient(create_app(lambda: down)) as down_client:
        response = down_client.get("/health")
    assert response.status_code == 503 and response.json()["checks"] == {"database": "ConnectionError"}
```

`/health/live` is ok; `/health` with no probes is ok with empty checks; with a failing probe it is 503 and names only the error class.

```python
def test_an_unexpected_error_is_a_generic_500_and_the_container_is_closed_on_shutdown(tmp_path):
    world = build_world(tmp_path)
    closed: list[bool] = []

    class Boom:
        def execute(self) -> None:
            raise RuntimeError("secret internals")

    broken = dataclasses.replace(world.services, worklist=Boom(), close=lambda: closed.append(True))  # type: ignore[arg-type]
    with TestClient(create_app(lambda: broken), raise_server_exceptions=False) as test_client:
        response = test_client.get("/v1/worklist")
        assert response.status_code == 500 and "secret" not in response.text
        assert response.json()["error"]["code"] == "internal_error"
    assert closed == [True]
```

The worklist use case is replaced by an object whose `execute` raises. The answer is 500 with the code `internal_error` and the text never contains `secret`; when the app shuts down the container's `close` was called once. The comment `# type: ignore[arg-type]` silences the type checker for this deliberate substitution.

```python
def test_parse_date_accepts_iso_and_refuses_the_rest():
    assert cli.parse_date("2026-10-20") == DAY
    with pytest.raises(InvalidDateError):
        cli.parse_date("20/10/2026")
```

`2026-10-20` parses; `20/10/2026` raises `InvalidDateError`.

```python
def test_the_parser_requires_a_command_and_a_date():
    parser = cli._parser()
    assert parser.parse_args(["reconcile", "--date", "2026-10-20"]).date == DAY
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["reconcile"])
```

The parser reads `reconcile --date 2026-10-20` into a date; no arguments, or a missing date, make it exit (`SystemExit`).

```python
def test_the_commands_print_the_lessons_lines(tmp_path, capsys):
    world: World = build_world(tmp_path)
    args = argparse.Namespace(date=DAY, payments=500, seed=43, prefix="ptx_", plant=True)
    assert cli.cmd_simulate(world.services, args) == 0
    assert cli.cmd_reconcile(world.services, args) == 0
    assert cli.cmd_resolve(world.services, args) == 0
    assert cli.cmd_report(world.services, args) == 0
    assert cli.cmd_worklist(world.services, args) == 0
    out = capsys.readouterr().out
    assert "the process died after the provider captured: ptx_0200" in out
    assert "breaks found: 6" in out and "  ptx_0215  duplicate_on_our_side" in out
    assert "ptx_0112: fee corrected, +0.18 posted, ledger balanced" in out
    assert "ptx_0400  amount_mismatch" in out
```

`capsys` is a pytest fixture that captures printed output. The test runs simulate, reconcile, resolve, report and worklist for the 500-payment planted day and checks that the printed text contains the lesson's lines (the crash at `ptx_0200`, six breaks, the duplicate, the fee correction message, the amount mismatch).

```python
def test_an_empty_worklist_says_so(tmp_path, capsys):
    world = build_world(tmp_path)
    assert cli.cmd_worklist(world.services, argparse.Namespace()) == 0
    assert "the worklist is empty" in capsys.readouterr().out
```

An empty worklist prints `the worklist is empty`.

```python
def test_the_demo_runs_the_whole_scenario_and_checks_itself(tmp_path, capsys):
    world = build_world(tmp_path)
    assert cli.cmd_demo(world.services, argparse.Namespace(payments=500)) == 0
    out = capsys.readouterr().out
    assert "0 breaks found" in out and "6 breaks found, 6 classified" in out
    assert "ptx_0112: already resolved" in out and "identical" in out
    assert "cleared carried differences: ptx_0050" in out
    assert "ledger sum across every posting: 0.00" in out
```

The whole seven-step demo runs on the fakes and returns 0; its output contains the expected lines.

```python
def test_expect_raises_the_scenario_error():
    with pytest.raises(cli.ScenarioError, match="boom"):
        cli.expect(False, "boom")
    cli.expect(True, "never shown")
```

`expect(False, ...)` raises `ScenarioError` with the message; `expect(True, ...)` does nothing.

## Try it yourself

1. Run all unit tests:

   ```bash
   make test
   ```

   Expected: the unit tests pass and the coverage report shows the domain and application at or above 95 percent.

2. Run one file, verbosely:

   ```bash
   .venv/bin/python -m pytest tests/unit/domain/test_reconciliation.py -v
   ```

   Expected: each test name followed by `PASSED`.

3. Break something on purpose: in `src/payments_reconciliation/domain/reconciliation.py` change `Cause.TIMING` to `Cause.AMOUNT_MISMATCH` in the `HANDLING` table, run `make test`, read the failing assertion, then undo the change.

## Mistakes people make

- **Tests that depend on today's date or on random values.** Here the clock is fixed and the simulator uses a seed.
- **Fakes that are kinder than the real thing.** The in-memory unit of work restores a snapshot, so a failed block really leaves nothing behind; the integration tests check the same promises on PostgreSQL.
- **Asserting only that no exception happened.** Each test checks values: causes, statuses, postings and totals.
- **Sharing state between tests.** Each test gets its own world and temporary folder.

## Check yourself

1. What is a fixture? *A function that prepares something a test needs; pytest passes it in by parameter name.*
2. How does the fake database imitate a rollback? *It copies the state before the block and restores it if the block raises.*
3. Why does a test use a seed for the simulated day? *So the same day, and the same six breaks, appear every time.*
4. What does `tmp_path` give a test? *A new empty temporary folder.*
5. Why test with a 60-payment day as well as the 500-payment day? *It is faster and reaches the same six causes at smaller positions.*

## Further reading

- pytest documentation: https://docs.pytest.org/en/stable/
- pytest fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- pytest parametrize: https://docs.pytest.org/en/stable/how-to/parametrize.html
- Python 3.12 `threading`: https://docs.python.org/3.12/library/threading.html
