# Python walkthrough: the application package, line by line

## What it is

This note reads every line of the `application` package: the use cases (record a capture, reconcile a day, resolve breaks, build the daily report, read the worklist, check health), the ledger operation they share, the views that turn results into JSON-ready shapes, and the ports that describe what the use cases need from the outside world.

A **use case** is one thing a user can ask the service to do. It coordinates the domain objects and the outside world but contains no web or database code itself.

## Why this repository uses it

The lesson's central guarantee is that every difference between the ledger and the provider's report is found, labeled and handled exactly once, and that running the same day again changes nothing. That behavior is built here, on top of the pure rules in the domain. Because the use cases talk to the database, the files and the provider only through ports, the same code runs against PostgreSQL in production and against in-memory fakes in the fast tests.

## Where it appears

| File | What it is |
| --- | --- |
| `src/payments_reconciliation/application/__init__.py` | The application package. |
| `src/payments_reconciliation/application/logs.py` | The log field helper. |
| `src/payments_reconciliation/application/views.py` | JSON-ready shapes. |
| `src/payments_reconciliation/application/ledger_ops.py` | Recording a known capture, shared by the capture path and the resolvers. |
| `src/payments_reconciliation/application/record_capture.py` | The capture path of the service being reconciled. |
| `src/payments_reconciliation/application/reconcile_day.py` | Reconcile one business day. |
| `src/payments_reconciliation/application/resolve_breaks.py` | Resolve breaks safely and exactly once. |
| `src/payments_reconciliation/application/daily_report.py` | The daily report and its text. |
| `src/payments_reconciliation/application/worklist.py` | The breaks that need a person. |
| `src/payments_reconciliation/application/health.py` | The health check. |
| `src/payments_reconciliation/application/ports.py` | What the use cases need from outside. |

## How it works underneath

A use case's `execute` method opens a **transaction** through the unit-of-work factory (a group of database changes that all happen or none do), reads what it needs through repositories, applies the domain's pure functions, writes the result and lets the transaction commit when the `with` block ends. If an exception escapes, the transaction rolls back. The safe-to-repeat behavior comes from three habits: stores refuse a repeat of the same natural key (a unique constraint), identifiers made with `uuid5` are the same for the same input, and the resolvers check whether their work is already done before doing it. The resolvers lock the provider transaction (an advisory lock) so two of them take turns.

## Walkthrough of this repository's use



### `src/payments_reconciliation/application/__init__.py` (1 lines)

```python
"""Use cases and the ports they need. Depends on the domain only."""
```

The docstring of the `application` package: the use cases (record a capture, reconcile a day, resolve breaks, produce the daily report) and the "ports" (descriptions of what the use cases need from the outside world).

### `src/payments_reconciliation/application/logs.py` (10 lines)

```python
"""Structured-log helper shared by the use cases: `logger.info("msg", extra=fields(payment_id=...))`."""

from __future__ import annotations

from typing import Any


def fields(**values: Any) -> dict[str, Any]:
    """Attach key=value pairs to a log record. Never pass card data, tokens or secrets."""
    return {"fields": values}
```

`logs.py` is the whole helper behind every structured log line in the use cases. `from typing import Any` imports `Any`, a type hint meaning "a value of any type".

`def fields(**values: Any) -> dict[str, Any]:` has a parameter written `**values`. The two stars collect every **keyword argument** (`name=value` written at the call) into one dictionary called `values`. So `fields(payment_id="p1", amount="42.50")` receives `{"payment_id": "p1", "amount": "42.50"}`. The function returns `{"fields": values}`: the logging module will attach it to the log record under the name `fields`, and the formatter (see `logging-and-logfmt.md`) prints each pair as `key=value`. The docstring warns callers never to pass card data, tokens or secrets.

### `src/payments_reconciliation/application/views.py` (86 lines)

```python
"""JSON-ready shapes for the API and the command-line program."""

from __future__ import annotations

from typing import Any

from payments_reconciliation.application.daily_report import DailyReport, Line, render
from payments_reconciliation.application.ports import RunRecord, StoredBreak
from payments_reconciliation.application.resolve_breaks import Resolution
```

JSON-ready shapes for the API and the command-line program: plain dictionaries of text, numbers and lists that can be written as JSON. `Any` is the type hint for "any value". The imports bring in the report types and the stored-break types.

```python
def break_view(stored: StoredBreak) -> dict[str, Any]:
    """A stored break."""
    finding = stored.finding

    def minor(amount: Any) -> int | None:
        return None if amount is None else int(amount.minor)

    return {
        "id": stored.id,
        "run_date": stored.run_date.isoformat(),
        "provider_txn_id": finding.provider_txn_id,
        "kind": finding.kind.value,
        "cause": finding.cause.value,
        "handling": finding.handling.value,
        "status": stored.status.value,
        "ledger_amount_minor": minor(finding.ledger_amount),
        "settlement_amount_minor": minor(finding.settlement_amount),
        "ledger_fee_minor": minor(finding.ledger_fee),
        "settlement_fee_minor": minor(finding.settlement_fee),
        "detail": finding.detail,
        "note": stored.note,
    }
```

A stored break as a dictionary. The inner function `minor` returns the amount in minor units, or `None` when that side has no amount (`None if amount is None else int(amount.minor)` is a one-line `if`/`else` expression). The dictionary lists the id, the run date as ISO text, the provider id, the kind, the cause, how it is handled, its status, the four amounts, the explanation and the note.

```python
def run_view(run: RunRecord) -> dict[str, Any]:
    """A run's first-pass numbers."""
    return {
        "run_date": run.run_date.isoformat(),
        "currency": run.currency,
        "ledger_payments": run.ledger_payments,
        "settlement_rows": run.settlement_rows,
        "settled_total_minor": run.settled_total.minor,
        "refunded_total_minor": run.refunded_total.minor,
        "clean_transactions": run.clean_transactions,
        "clean_total_minor": run.clean_total.minor,
        "break_count": run.break_count,
    }
```

The first-pass numbers of a run: counts of ledger payments and settlement rows, the settled and refunded totals and the clean totals in minor units, and the number of breaks.

```python
def resolution_view(resolution: Resolution) -> dict[str, Any]:
    """One resolution."""
    return {
        "provider_txn_id": resolution.stored.finding.provider_txn_id,
        "cause": resolution.stored.finding.cause.value,
        "outcome": resolution.outcome.value,
        "message": resolution.message,
    }
```

One resolution: the transaction, the cause, the outcome text and the message the report repeats.

```python
def _line(line: Line) -> dict[str, Any]:
    return {"provider_txn_id": line.provider_txn_id, "cause": line.cause, "text": line.text}
```

A private helper for one report line.

```python
def report_view(report: DailyReport) -> dict[str, Any]:
    """The daily report as JSON, with its text rendering."""
    return {
        "business_date": report.business_date.isoformat(),
        "currency": report.currency,
        "run": run_view(report.run),
        "settled_rows": report.settled_rows,
        "settled_total_minor": report.settled_total.minor,
        "buckets_minor": {name: amount.minor for name, amount in report.buckets.items()},
        "balances": report.balances,
        "clean_transactions": report.clean_transactions,
        "auto_resolved": [_line(line) for line in report.auto_resolved],
        "resolved": [_line(line) for line in report.resolved],
        "carried": [_line(line) for line in report.carried],
        "settled_later": [_line(line) for line in report.settled_later],
        "cleared_carried": [_line(line) for line in report.cleared_carried],
        "worklist": [_line(line) for line in report.worklist],
        "open": [_line(line) for line in report.open_items],
        "ledger_difference_minor": report.ledger_difference.minor,
        "ledger_sum_minor": report.ledger_sum.minor,
        "text": render(report),
    }
```

The daily report as JSON. Each section is a list built with a **list comprehension** (`[_line(line) for line in report.carried]`: one dictionary per line). `buckets_minor` is a **dict comprehension** from bucket name to minor units. `text` is the human-readable report produced by `render`.

### `src/payments_reconciliation/application/ledger_ops.py` (38 lines)

```python
"""Ledger operations shared by the capture path and the resolvers: each posts whole, balanced entries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from payments_reconciliation.application.ports import UnitOfWork
from payments_reconciliation.domain.ledger import capture_entries
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState
```

The docstring: ledger operations shared by the capture path and the resolvers; each posts whole, balanced entries. `UnitOfWork` is the transaction port; `capture_entries` builds the six rows of a capture.

```python
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
```

`record_known_capture` inserts a captured payment and its capture entries in the **caller's** transaction (it never opens its own, so the caller decides what commits together). The `*` makes every argument after `uow` keyword-only. `idempotency_key: str | None = None` is optional: the service's own capture path passes one, a resolver that inserts a payment from the report does not.

```python
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
```

Builds the payment row as a `LedgerPayment` in the state `CAPTURED`, with nothing refunded yet (`Money.zero(amount.currency)`).

```python
    uow.payments.add(payment, idempotency_key)
    uow.ledger.post(payment_id, "CAPTURE", "capture", capture_entries(amount, fee), captured_at)
    return payment
```

Stores the payment, then appends its capture entries to the ledger under the operation name `CAPTURE` and the reference `capture`. Together with the leg that is the unique key the database enforces, so the same capture can never be booked twice for one payment. The function returns the payment.

### `src/payments_reconciliation/application/record_capture.py` (60 lines)

```python
"""The capture path of the service this project reconciles: capture at the provider, record it in the ledger."""

from __future__ import annotations

import logging
from collections.abc import Callable

from payments_reconciliation.application.ledger_ops import record_known_capture
from payments_reconciliation.application.logs import fields
from payments_reconciliation.application.ports import CaptureResult, Clock, IdSource, PaymentProvider, UnitOfWorkFactory
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource
```

The capture path of the service this project reconciles: capture at the provider, record it in the ledger. `logging` writes log lines; `Callable` is the type of a function value; `fields` is the log helper; the ports and `record_known_capture` come from the sibling files.

```python
logger = logging.getLogger(__name__)
```

A logger named after the module, so log lines can be traced to this file.

```python
class RecordCapture:
    """Capture `amount` at the provider and record the payment and its six ledger rows in one transaction.

    Idempotent on the request key: a repeat finds the payment already recorded and returns it. `after_provider_call`
    runs between the provider's answer and the database write; the simulation uses it to crash there, which leaves
    money moved at the provider and nothing in the ledger.
    """
```

The class docstring says what it does: capture at the provider and record the payment with its six ledger rows in **one transaction**. It is idempotent on the request key: a repeat finds the payment already recorded and returns it. `after_provider_call` is a hook that runs between the provider's answer and the database write; the simulation uses it to crash exactly there, which leaves money moved at the provider and nothing in the ledger, the situation reconciliation exists to find.

```python
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
```

The constructor receives everything from outside (the transaction factory, the provider, the clock, the id source and the optional hook) and keeps it in attributes whose names start with `_`, the Python habit for "private". Nothing is created here, which is what makes the class testable. `Callable[[CaptureResult], None] | None` means "a function taking a `CaptureResult` and returning nothing, or nothing at all".

```python
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
```

The work, inside `with self._uow_factory() as uow:` (a transaction that commits if the block ends normally and rolls back on an exception). First it looks for a payment already recorded under this key (`by_key`) and returns it if found: a retry gets the first answer. Otherwise it reads the time, calls the provider with the same key (so the provider also captures only once), runs the hook if there is one, and records the payment with `record_known_capture` using a fresh id, the provider's transaction id and fee, and the source `SERVICE`. A log line `captured` is written with the transaction id and the formatted amount. If anything raises after the provider call, the transaction rolls back and the ledger is unchanged, while the provider has already moved the money.

### `src/payments_reconciliation/application/reconcile_day.py` (173 lines)

```python
"""Reconcile one business day: read the provider's file, match it against the ledger, classify and store the breaks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from payments_reconciliation.application.logs import fields
from payments_reconciliation.application.ports import (
    BreakStatus,
    Clock,
    RunRecord,
    SettlementFiles,
    StoredBreak,
    UnitOfWork,
    UnitOfWorkFactory,
)
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.reconciliation import Handling, LedgerView, MatchResult, reconcile
from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, parse_csv
```

Reconcile one business day: read the provider's file, match it against the ledger, classify and store the breaks. The imports bring in `dataclass`, the date and time types, the ports and the domain's `reconcile`.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
_UNRESOLVED = {BreakStatus.OPEN, BreakStatus.NEEDS_REVIEW, BreakStatus.CARRIED}
```

A set of the statuses meaning "not yet settled by anything": `OPEN`, `NEEDS_REVIEW` and `CARRIED`.

```python
@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """What a run found, and the stored breaks it left."""

    run: RunRecord
    result: MatchResult
    breaks: list[StoredBreak]
    cleared_carried: list[StoredBreak]
```

What a run returns: the run record, the domain's match result, the stored breaks it left and the carried breaks that cleared today.

```python
class ReconcileDay:
    """Join the ledger and the settlement report of one day on the provider's transaction id.

    Running a day again is safe: breaks are keyed by (day, transaction, kind, cause), a resolved break stays
    resolved, and a break that no longer reproduces is cleared.
    """
```

The class docstring states the safety promise: running a day again is safe, because breaks are keyed by (day, transaction, kind, cause), a resolved break stays resolved, and a break that no longer reproduces is cleared.

```python
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        files: SettlementFiles,
        clock: Clock,
        *,
        currency: str,
        cutoff: time,
        max_carry_days: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._files = files
        self._clock = clock
        self._currency = currency
        self._cutoff = cutoff
        self._max_carry_days = max_carry_days
```

Keeps the transaction factory, the file store, the clock and three settings that come after the `*` (so they must be named): the currency, the provider's cutoff time and how many days a timing difference may be carried.

```python
    def execute(self, business_date: date) -> ReconcileOutcome:
        """Run the day. Raises `SettlementMissingError` when the provider's file has not arrived."""
        report = parse_csv(self._files.read(business_date), business_date, self._currency)
        with self._uow_factory() as uow:
            now = self._clock.now()
            carried = {stored.finding.provider_txn_id: stored for stored in uow.breaks.carried()}
            carried_days = {txn: (business_date - stored.run_date).days for txn, stored in carried.items()}
            ledger = self._ledger_view(uow, business_date, set(carried))
            ledger = self._without_settled_later(uow, business_date, ledger)
            result = reconcile(
                ledger, report, carried_days=carried_days, cutoff=self._cutoff, max_carry_days=self._max_carry_days
            )
            run = self._run_record(business_date, report, ledger, result, now)
            uow.runs.save(run)  # first: the breaks below refer to it
            cleared = self._clear_carried(uow, carried, result, business_date, now)
            self._store(uow, business_date, result, carried, now)
            stored_breaks = uow.breaks.for_run(business_date)
        logger.info(
            "reconciled",
            extra=fields(
                business_date=business_date.isoformat(), breaks=len(result.breaks), clean=run.clean_transactions
            ),
        )
        return ReconcileOutcome(run, result, stored_breaks, cleared)
```

The steps, in order. (1) Read and parse the day's file (a missing file raises `SettlementMissingError`). (2) Inside one transaction: read the time; load the currently carried breaks into a dictionary keyed by transaction id; compute for each how many days it has waited (`(business_date - stored.run_date).days`); build the ledger view (the day's captures plus the payments of carried transactions); remove payments whose timing difference already cleared in a later report; run the pure `reconcile`; build and save the run record (**first**, because the breaks refer to it); clear carried breaks that have now settled; store the findings; read back the stored breaks. (3) After the transaction commits, log how many breaks and clean transactions. The method returns a `ReconcileOutcome`.

```python
    def _ledger_view(self, uow: UnitOfWork, business_date: date, carried_ids: set[str]) -> LedgerView:
        payments = {payment.id: payment for payment in uow.payments.captured_on(business_date)}
        payments.update({payment.id: payment for payment in uow.payments.by_txns(carried_ids)})
        refunds = uow.refunds.recorded_on(business_date, self._currency)
        return LedgerView(tuple(payments.values()), refunds)
```

Our side of the day. A dictionary comprehension maps payment id to payment for the payments captured on the day; `payments.update({...})` adds the payments for carried transaction ids (a dictionary can hold each id only once, so nothing is counted twice). The refunds recorded that day come from the refund store. The result is a `LedgerView` with the payments as a tuple.

```python
    @staticmethod
    def _without_settled_later(uow: UnitOfWork, business_date: date, ledger: LedgerView) -> LedgerView:
        """Leave out payments whose timing difference already cleared in a later report, so a re-run stays quiet."""
        settled_later = {
            stored.finding.provider_txn_id
            for stored in uow.breaks.for_run(business_date)
            if stored.status is BreakStatus.CLEARED and stored.cleared_on is not None
        }
        kept = tuple(payment for payment in ledger.payments if payment.provider_txn_id not in settled_later)
        return LedgerView(kept, ledger.refunds)
```

A `@staticmethod` (the line above) is a method that does not use `self`. The docstring: leave out payments whose timing difference already cleared in a later report, so a re-run of an old day stays quiet. A **set comprehension** collects the transaction ids of this run's breaks that are `CLEARED` with a clearing date; `kept` is the ledger's payments minus those; a new `LedgerView` is returned with the same refunds.

```python
    def _clear_carried(
        self, uow: UnitOfWork, carried: dict[str, StoredBreak], result: MatchResult, business_date: date, now: datetime
    ) -> list[StoredBreak]:
        matched_ids = {item.provider_txn_id for item in result.matched if item.kind is SettlementKind.SETTLED}
        cleared: list[StoredBreak] = []
        for txn_id, stored in carried.items():
            if txn_id in matched_ids and txn_id not in result.break_ids():
                note = f"settled in the report of {business_date.isoformat()}; no action was needed"
                uow.breaks.update(stored.id, BreakStatus.CLEARED, note, now, cleared_on=business_date)
                cleared.append(stored)
        return cleared
```

For each carried break, if its transaction now appears as a matched settled row and has no break today, the carried break is updated to `CLEARED` with a note saying when it settled and that no action was needed, and added to the returned list. This is the happy ending of a timing difference.

```python
    def _store(
        self, uow: UnitOfWork, business_date: date, result: MatchResult, carried: dict[str, StoredBreak], now: datetime
    ) -> None:
        found_keys: set[tuple[str, SettlementKind, str]] = set()
        for finding in result.breaks:
            if finding.provider_txn_id in carried and finding.cause.value == "timing":
                waiting = carried[finding.provider_txn_id]
                uow.breaks.refresh(waiting.id, finding, now)  # still waiting: the original row keeps counting
                continue
            if finding.provider_txn_id in carried and finding.cause.value == "timing_overdue":
                waiting = carried[finding.provider_txn_id]
                uow.breaks.update(waiting.id, BreakStatus.ESCALATED, "waited too long; see the new break", now)
            found_keys.add((finding.provider_txn_id, finding.kind, finding.cause.value))
            existing = uow.breaks.find(business_date, finding.provider_txn_id, finding.kind, finding.cause.value)
            if existing is None:
                uow.breaks.insert(business_date, finding, self._initial_status(finding.handling), finding.detail, now)
            elif existing.status in _UNRESOLVED:
                uow.breaks.refresh(existing.id, finding, now)
            else:
                uow.breaks.update(existing.id, BreakStatus.OPEN, "reappeared after it was resolved", now)
                uow.breaks.refresh(existing.id, finding, now)
        for stored in uow.breaks.for_run(business_date):
            key = (stored.finding.provider_txn_id, stored.finding.kind, stored.finding.cause.value)
            if key not in found_keys and stored.status in {BreakStatus.OPEN, BreakStatus.NEEDS_REVIEW}:
                uow.breaks.update(stored.id, BreakStatus.CLEARED, "no longer differs on this run", now)
```

Saves what the run found, without ever creating a duplicate. `found_keys` collects the natural key of every finding. For each finding: if the transaction is already carried and the new finding is `timing` again, the original row is refreshed with today's numbers (`refresh`) and the loop moves on (`continue`), so the original row keeps counting its days. If it is carried and now `timing_overdue`, the old break is marked `ESCALATED` and a new break (below) holds the problem. Otherwise it looks for an existing break with the same natural key: none means insert it with an initial status; an unresolved one is refreshed; one that was already resolved is reopened (`OPEN`, note "reappeared after it was resolved") and refreshed. Finally, any stored break of this run that was `OPEN` or `NEEDS_REVIEW` but was not found this time is marked `CLEARED` ("no longer differs on this run").

```python
    @staticmethod
    def _initial_status(handling: Handling) -> BreakStatus:
        if handling is Handling.CARRY:
            return BreakStatus.CARRIED
        if handling is Handling.HUMAN:
            return BreakStatus.NEEDS_REVIEW
        return BreakStatus.OPEN
```

The first status of a new break follows its handling: a carried cause starts `CARRIED`, a human cause starts `NEEDS_REVIEW`, anything else (automatic or from-report) starts `OPEN`, waiting for the resolvers.

```python
    def _run_record(
        self, business_date: date, report: SettlementReport, ledger: LedgerView, result: MatchResult, now: datetime
    ) -> RunRecord:
        broken = result.break_ids()
        clean = [
            item
            for item in result.matched
            if item.kind is SettlementKind.SETTLED and item.provider_txn_id not in broken
        ]
        clean_total = Money.zero(self._currency)
        for item in clean:
            clean_total = clean_total + item.amount
        return RunRecord(
            run_date=business_date,
            currency=self._currency,
            ledger_payments=sum(1 for payment in ledger.payments if payment.active),
            settlement_rows=len(report.rows),
            settled_total=report.settled_total,
            refunded_total=report.refunded_total,
            clean_transactions=len(clean),
            clean_total=clean_total,
            break_count=len(result.breaks),
            ran_at=now,
        )
```

Builds the run's first-pass numbers. `broken` is the set of ids with any break; `clean` lists the matched settled items that are not broken (a list comprehension with a condition). The clean total is summed starting from zero. The `RunRecord` has: the date, the currency, how many active ledger payments were compared (`sum(1 for ... if payment.active)` counts them), how many report rows, the settled and refunded totals from the report, the clean count and total, the number of breaks and the time.

### `src/payments_reconciliation/application/resolve_breaks.py` (188 lines)

```python
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
```

Resolve the breaks that can be resolved safely, each exactly once, never by calling the provider again. New imports: `uuid` (to make repeatable ids), `time` and `UTC` for times, and the domain's ledger functions for fee correction, refunds and reversal.

```python
logger = logging.getLogger(__name__)
```

A module logger.

```python
# Missing captures first, so a refund for the same transaction finds its payment; duplicates before fees.
_ORDER = [
    Cause.MISSING_CAPTURE,
    Cause.DUPLICATE_ON_OUR_SIDE,
    Cause.FEE_DISCREPANCY,
    Cause.PROVIDER_ONLY_REFUND,
]
```

The order the resolvers run in. The comment gives the reason: missing captures first, so a refund for the same transaction finds its payment; duplicates before fees.

```python
_NAMESPACE = uuid.UUID("6f1c7d5e-0b1a-4c1e-9a53-5d7b7a1c0001")
```

A fixed UUID used as a **namespace**. `uuid.uuid5(namespace, name)` always makes the same UUID for the same namespace and name, so inserting the same missing capture twice produces the same id, which is what makes the resolvers safe to run twice.

```python
class Outcome(StrEnum):
    """What a resolution did."""

    RESOLVED = "resolved"
    ALREADY_RESOLVED = "already resolved"
    CARRIED = "carried to the next report"
    NEEDS_REVIEW = "needs a human"
```

What a resolution did, as text the report can repeat: resolved, already resolved, carried to the next report, or needs a human.

```python
@dataclass(frozen=True, slots=True)
class Resolution:
    """One break's resolution: what happened, in words the daily report can repeat."""

    stored: StoredBreak
    outcome: Outcome
    message: str
```

One break's resolution: the break, the outcome and a message in plain words.

```python
_Resolver = Callable[[UnitOfWork, StoredBreak, datetime], tuple[Outcome, str]]
```

The type of a resolver function: it receives the transaction, the break and the time and returns an `(Outcome, message)` pair.

```python
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
```

Applies the rule for each break of a run. The docstring promises that a second pass changes nothing. The constructor keeps the factory and clock and builds a dictionary from each automatically resolvable cause to the method that resolves it.

```python
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
```

First collects the ids of the run's breaks in one short transaction. Then `resolve` is called for each id (each in its **own** transaction, so one failure does not undo the others). `order` maps each cause to its position in `_ORDER` (`enumerate` numbers the list), and the result is sorted by that position, then by transaction id; causes not in `_ORDER` come last (`len(order)`). `key=lambda item: ...` is a small unnamed function that tells `sorted` what to sort by.

```python
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
```

Resolves one break in its own transaction under a lock on the break row (`get_for_update`), so two resolvers cannot work on the same break at once. A break that does not exist raises `LookupError`. Then the cases, in order: a break already `RESOLVED`, `AUTO_RESOLVED`, `CLEARED` or `ESCALATED` returns `ALREADY_RESOLVED` with its stored note and does nothing; a carry-handled break returns `CARRIED` with a note; a human-handled break returns `NEEDS_REVIEW` with a note that replaces underscores in the cause by spaces (`cause.value.replace("_", " ")`). Otherwise the resolver for the cause runs. If it asks for a human the break is updated to `NEEDS_REVIEW`; otherwise to `AUTO_RESOLVED` (automatic handling) or `RESOLVED`. A log line records the transaction, cause and outcome, and the resolution is returned.

```python
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
```

Fee discrepancy. It locks the transaction, finds its active payments, and refuses (`NEEDS_REVIEW`) unless there is exactly one and the report supplies a fee. `fee_correction_entry` returns `None` if the fees already agree (`ALREADY_RESOLVED`). Otherwise `delta` is the difference; one entry is posted under the operation `FEE_CORRECTION` with a reference containing the new fee, so it can only be posted once; the payment is saved with the new fee. The message shows the sign and the absolute amount (`abs(delta.minor)`).

```python
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
```

Missing capture. After locking, if any payment exists for the transaction the work is already done. The two `assert` lines tell the type checker (and fail loudly if wrong) that the report's amount and fee are present. Then `record_known_capture` inserts the payment from the report's own numbers, with an id made by `uuid5` from the transaction id (the same every time), the time set to midnight UTC of the run day (`datetime.combine(..., time(0, 0), tzinfo=UTC)`) and the source `RECONCILIATION`. Six ledger rows are posted in the same transaction.

```python
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
```

Duplicate on our side. After locking, the active payments are sorted by capture time then id so the oldest is the one kept. With one or none nothing remains to do. For every extra payment a reversal is posted (operation `REVERSAL`, reference naming the kept payment) and the payment is saved as `DUPLICATE_REVERSED`. Nothing is deleted: the ledger shows the capture and its reversal.

```python
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
```

Provider-only refund. It needs exactly one active payment and the report's amount. The reference `settlement-refund:<id>` makes the refund unique; if a refund with it exists the work is done. Otherwise, in order: save the payment with the refund added (which refuses more than was captured), add the refund record with a repeatable id from `uuid5`, and post the two refund entries (four rows) at midnight UTC of the run day.

### `src/payments_reconciliation/application/daily_report.py` (224 lines)

```python
"""The daily report: what matched, what rules cleared, what was fixed from the report, and what needs a person."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from payments_reconciliation.application.ports import (
    BreakStatus,
    Clock,
    RunRecord,
    SettlementFiles,
    StoredBreak,
    UnitOfWorkFactory,
)
from payments_reconciliation.domain.errors import ReconciliationError
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind, SettlementRow, parse_csv
```

The daily report: what matched, what rules cleared, what was fixed from the report, and what needs a person. New imports: `Callable`, `field` (a default for a dataclass field) and `date`.

```python
class ReportNotAvailableError(ReconciliationError):
    """The day has not been reconciled yet."""

    code = "report_not_available"
```

A new error, defined next to the code that raises it: the day has not been reconciled yet. It has its own `code`.

```python
@dataclass(frozen=True, slots=True)
class Line:
    """One transaction in a report section."""

    provider_txn_id: str
    cause: str
    text: str
    amount_minor: int
```

One transaction in a report section: its id, the cause, an explanation and an amount.

```python
@dataclass(frozen=True, slots=True)
class DailyReport:
    """The report for one business day. `buckets` partition the settled total, so the sum can be checked."""

    business_date: date
    currency: str
    run: RunRecord
    settled_rows: int
    settled_total: Money
    refunded_total: Money
    buckets: dict[str, Money]
    clean_transactions: int
    ledger_difference: Money
    ledger_sum: Money
    auto_resolved: list[Line] = field(default_factory=list)
    resolved: list[Line] = field(default_factory=list)
    carried: list[Line] = field(default_factory=list)
    settled_later: list[Line] = field(default_factory=list)
    cleared_carried: list[Line] = field(default_factory=list)
    worklist: list[Line] = field(default_factory=list)
    open_items: list[Line] = field(default_factory=list)
```

The report for one business day. The docstring says the `buckets` partition the settled total, so the sum can be checked. The first fields have no default; the list fields after them use `field(default_factory=list)` so each report gets its own empty list.

```python
    @property
    def balances(self) -> bool:
        """True when the buckets add up to the settlement report's settled total."""
        total = Money.zero(self.currency)
        for amount in self.buckets.values():
            total = total + amount
        return total == self.settled_total
```

The property `balances` adds up the buckets from zero and returns whether the total equals the settled total. This is the report's self-check: if a transaction were counted twice or lost, it would say `DOES NOT BALANCE`.

```python
class BuildDailyReport:
    """Assemble the report for a reconciled day from its run, its breaks and the provider's file."""

    def __init__(self, uow_factory: UnitOfWorkFactory, files: SettlementFiles, clock: Clock, currency: str) -> None:
        self._uow_factory = uow_factory
        self._files = files
        self._clock = clock
        self._currency = currency
```

Assembles the report for a reconciled day from its run, its breaks and the provider's file.

```python
    def execute(self, business_date: date) -> DailyReport:
        """Raises `ReportNotAvailableError` when the day has no run."""
        report = parse_csv(self._files.read(business_date), business_date, self._currency)
        with self._uow_factory() as uow:
            run = uow.runs.get(business_date)
            if run is None:
                raise ReportNotAvailableError(f"{business_date.isoformat()} has not been reconciled")
            breaks = uow.breaks.for_run(business_date)
            cleared = uow.breaks.cleared_on(business_date)
            ledger_by_txn: dict[str, Money] = {}
            for payment in uow.payments.by_txns({row.provider_txn_id for row in report.rows}):
                if payment.active:
                    previous = ledger_by_txn.get(payment.provider_txn_id, Money.zero(self._currency))
                    ledger_by_txn[payment.provider_txn_id] = previous + payment.amount
            ledger_sum = uow.ledger.total(self._currency)
        return self._build(run, report.rows, breaks, cleared, ledger_by_txn, ledger_sum)
```

Reads and parses the file, then in a transaction reads the day's run (no run means `ReportNotAvailableError`), the run's breaks, the breaks that cleared that day, and the ledger's active payments for the report's transaction ids, summed per id into `ledger_by_txn`, plus the ledger total. After the transaction it calls `_build`.

```python
    def _build(
        self,
        run: RunRecord,
        rows: tuple[SettlementRow, ...],
        breaks: list[StoredBreak],
        cleared: list[StoredBreak],
        ledger_by_txn: dict[str, Money],
        ledger_sum: Money,
    ) -> DailyReport:
        settled = {row.provider_txn_id: row.amount for row in rows if row.kind is SettlementKind.SETTLED}
        status_of: dict[str, BreakStatus] = {}
        lines: dict[BreakStatus, list[Line]] = {status: [] for status in BreakStatus}
        for stored in breaks:
            finding = stored.finding
            line = Line(finding.provider_txn_id, finding.cause.value, stored.note or finding.detail, 0)
            lines[stored.status].append(line)
            current = status_of.get(finding.provider_txn_id)
            status_of[finding.provider_txn_id] = _worse(current, stored.status)
        cleared_ids = {stored.finding.provider_txn_id for stored in cleared}
        names = {
            BreakStatus.AUTO_RESOLVED: "auto_resolved",
            BreakStatus.RESOLVED: "resolved",
            BreakStatus.NEEDS_REVIEW: "worklist",
            BreakStatus.OPEN: "open",
            BreakStatus.CLEARED: "resolved",
            BreakStatus.CARRIED: "carried",
            BreakStatus.ESCALATED: "worklist",
        }
        buckets = {
            name: Money.zero(self._currency)
            for name in ("clean", "cleared_carried", "auto_resolved", "resolved", "worklist", "open")
        }
        for txn_id, amount in settled.items():
            if txn_id in cleared_ids:
                bucket = "cleared_carried"
            elif txn_id in status_of:
                bucket = names[status_of[txn_id]]
            else:
                bucket = "clean"
            buckets[bucket] = buckets[bucket] + amount
        difference = Money.zero(self._currency)
        for txn_id, amount in settled.items():
            difference = difference + (amount - ledger_by_txn.get(txn_id, Money.zero(self._currency)))
        return DailyReport(
            business_date=run.run_date,
            currency=self._currency,
            run=run,
            settled_rows=sum(1 for row in rows if row.kind is SettlementKind.SETTLED),
            settled_total=run.settled_total,
            refunded_total=run.refunded_total,
            buckets=buckets,
            clean_transactions=run.clean_transactions,
            auto_resolved=lines[BreakStatus.AUTO_RESOLVED],
            resolved=lines[BreakStatus.RESOLVED],
            carried=lines[BreakStatus.CARRIED],
            settled_later=[
                Line(stored.finding.provider_txn_id, stored.finding.cause.value, stored.note, 0)
                for stored in breaks
                if stored.status is BreakStatus.CLEARED and stored.cleared_on is not None
            ],
            cleared_carried=[
                Line(stored.finding.provider_txn_id, stored.finding.cause.value, stored.note, 0) for stored in cleared
            ],
            worklist=lines[BreakStatus.NEEDS_REVIEW],
            open_items=lines[BreakStatus.OPEN],
            ledger_difference=difference,
            ledger_sum=ledger_sum,
        )
```

Builds the report from the pieces. `settled` maps each settled transaction to its amount. `lines` holds, for every possible status, a list of lines; each stored break becomes a `Line` (using the note, or the detail when the note is empty) in the list of its status, and `status_of` remembers the **worst** status per transaction using `_worse`. `names` maps each status to the bucket it feeds. `buckets` starts with six buckets at zero. Each settled transaction goes into exactly one bucket: `cleared_carried` if it was a carried break that cleared today, the bucket of its worst status if it had a break, otherwise `clean`. So the buckets partition the settled total. `difference` is the sum, over settled transactions, of the report amount minus the ledger amount. Finally the `DailyReport` is constructed from all of it, including the `settled_later` lines (breaks cleared because they settled in a later report) and the `cleared_carried` lines.

```python
_SEVERITY = [
    BreakStatus.NEEDS_REVIEW,
    BreakStatus.ESCALATED,
    BreakStatus.OPEN,
    BreakStatus.RESOLVED,
    BreakStatus.AUTO_RESOLVED,
    BreakStatus.CLEARED,
    BreakStatus.CARRIED,
]
```

The statuses ordered from the most to the least serious. A transaction with several breaks is shown under the most serious.

```python
def _worse(current: BreakStatus | None, new: BreakStatus) -> BreakStatus:
    if current is None:
        return new
    return current if _SEVERITY.index(current) <= _SEVERITY.index(new) else new
```

Returns the more serious of two statuses by comparing their positions in `_SEVERITY` (`list.index` gives the position); with no current status it returns the new one.

```python
def render(report: DailyReport) -> str:
    """The report as text, in the shape the lesson shows."""
    day = report.business_date.isoformat()

    def money(amount: Money) -> str:
        return f"{amount.format()} {report.currency}"

    out = [
        f"Daily reconciliation report for {day}",
        f"Settlement report: {report.settled_rows} settled rows totalling {money(report.settled_total)}, "
        f"refunds {money(report.refunded_total)}",
        f"Matched at the first pass: {report.clean_transactions} transactions, {money(report.run.clean_total)}",
    ]
    out.append("Auto-resolved: " + _join(report.auto_resolved, lambda x: f"{x.provider_txn_id} ({x.cause})"))
    out.append("Resolved from the report: " + _join(report.resolved, lambda x: f"{x.provider_txn_id} ({x.cause})"))
    out.append("Carried to the next day: " + _join(report.carried, lambda x: f"{x.provider_txn_id} ({x.cause})"))
    if report.settled_later:
        out.append(
            "Carried and settled later: " + _join(report.settled_later, lambda x: f"{x.provider_txn_id} ({x.text})")
        )
    if report.cleared_carried:
        out.append("Cleared today (carried earlier): " + _join(report.cleared_carried, lambda x: x.provider_txn_id))
    if report.open_items:
        out.append(
            "Still open (run resolve): " + _join(report.open_items, lambda x: f"{x.provider_txn_id} ({x.cause})")
        )
    out.append(
        "Worklist (needs a human): " + _join(report.worklist, lambda x: f"{x.provider_txn_id} ({x.cause}: {x.text})")
    )
    parts = " + ".join(
        f"{name.replace('_', ' ')} {money(amount)}" for name, amount in report.buckets.items() if not amount.is_zero()
    )
    out.append(
        f"Balance: settled total {money(report.settled_total)} = {parts}  "
        f"[{'balances' if report.balances else 'DOES NOT BALANCE'}]"
    )
    out.append(f"Difference against the ledger for these transactions: {money(report.ledger_difference)}")
    out.append(f"Ledger sum across every posting: {money(report.ledger_sum)}")
    return "\n".join(out)
```

Turns the report into the text of the lesson. The nested function `money` formats an amount with its currency. `out` is a list of lines: the heading, the settlement report summary, the matched-at-first-pass line, then lines for auto-resolved, resolved, carried, optionally settled later, cleared today and still open, and the worklist (the optional lines are added only when there is something to show). The balance line joins the non-zero buckets with ` + ` (a generator expression with a condition) and ends with `[balances]` or `[DOES NOT BALANCE]`. The last two lines show the difference against the ledger and the ledger sum across every posting. `"\n".join(out)` makes one text of the lines.

```python
def _join(lines: list[Line], show: Callable[[Line], str]) -> str:
    return ", ".join(show(line) for line in lines) if lines else "none"
```

Joins the shown form of each line with commas, or the word `none` for an empty list. `show` is a function passed in (`Callable[[Line], str]`) that decides how a line is displayed.

### `src/payments_reconciliation/application/worklist.py` (17 lines)

```python
"""The worklist: the breaks nothing in the code can resolve safely."""

from __future__ import annotations

from payments_reconciliation.application.ports import StoredBreak, UnitOfWorkFactory
```

The worklist: the breaks nothing in the code can resolve safely. The two imports are the stored-break type and the transaction factory.

```python
class GetWorklist:
    """Breaks waiting for a person, oldest first."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def execute(self) -> list[StoredBreak]:
        """Every break with status NEEDS_REVIEW."""
        with self._uow_factory() as uow:
            return uow.breaks.worklist()
```

A read-only use case. The constructor keeps the transaction factory; `execute` opens a transaction and returns `uow.breaks.worklist()`, every break with status `NEEDS_REVIEW`, oldest first.

### `src/payments_reconciliation/application/health.py` (23 lines)

```python
"""The health check: is every dependency reachable?"""

from __future__ import annotations

from payments_reconciliation.application.ports import HealthProbe
```

The docstring asks the question the file answers: is every dependency reachable? `HealthProbe` is a port (below).

```python
class CheckHealth:
    """Asks each probe and reports its status."""

    def __init__(self, probes: dict[str, HealthProbe]) -> None:
        self._probes = probes

    def execute(self) -> dict[str, str]:
        """Each dependency's status, `ok` or the error's type name."""
        results: dict[str, str] = {}
        for name, probe in self._probes.items():
            try:
                probe.check()
                results[name] = "ok"
            except Exception as error:  # a health check reports every failure; it must not raise
                results[name] = type(error).__name__
        return results
```

Asks every dependency whether it is reachable. `probes` is a dictionary from a name to a probe. `execute` loops over the probes, calls `probe.check()`, and records `"ok"`, or the **type name of the error** (`type(error).__name__`, such as `OperationalError`) when the probe raised. The `except Exception` is deliberately broad, as the comment says: a health check must report every failure and must not raise.

### `src/payments_reconciliation/application/ports.py` (300 lines)

```python
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
```

The docstring: the ports are what the use cases need from the outside world, written as interfaces the adapters implement. A **port** is a description of the services the application needs, without saying how they are provided. The imports include `Protocol` (a type that describes a shape), `Callable`, `Collection`, `Sequence`, `AbstractContextManager` (the type of something usable in a `with` block), `StrEnum` and the domain types.

```python
class Clock(Protocol):
    """The current time, injected so runs are deterministic."""

    def now(self) -> datetime:
        """A timezone-aware UTC time."""
        ...
```

`Clock` is a **Protocol**: a list of methods an object must have. A class need not inherit from it; having `now()` with the right type is enough, and mypy checks that. The body `...` (the **Ellipsis**) means "no code here". The time is injected so runs are deterministic.

```python
class IdSource(Protocol):
    """New identifiers, injected for the same reason."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        ...
```

`IdSource` provides new identifiers; injecting it lets tests use predictable ones.

```python
@dataclass(frozen=True, slots=True)
class CaptureResult:
    """The provider captured: its own transaction id, and the fee it charged."""

    provider_txn_id: str
    fee: Money
```

`CaptureResult` is the provider's answer to a capture: its own transaction id and the fee it charged.

```python
@dataclass(frozen=True, slots=True)
class RefundResult:
    """The provider returned money for a transaction."""

    provider_refund_id: str
```

`RefundResult` is the provider's id for a refund.

```python
class PaymentProvider(Protocol):
    """The payment provider, as the capture path uses it. Every call carries an idempotency key it honors."""

    def capture(self, *, idempotency_key: str, amount: Money, occurred_at: datetime) -> CaptureResult:
        """Capture `amount`; the same key returns the first result."""
        ...
```

`PaymentProvider` is the provider as the capture path uses it: `capture` and `refund`, each with a keyword-only idempotency key the provider honors (the same key returns the first result).

```python
    def refund(
        self, *, idempotency_key: str, provider_txn_id: str, amount: Money, occurred_at: datetime
    ) -> RefundResult:
        """Refund `amount` of a transaction; the same key returns the first result."""
        ...
```

The second method: refund `amount` of the transaction `provider_txn_id`.

```python
class SettlementFiles(Protocol):
    """Where the provider's daily settlement files arrive."""

    def read(self, business_date: date) -> str:
        """The file's text. Raises `SettlementMissingError` when the day has none."""
        ...
```

Where the provider's daily settlement files arrive. `read` returns a day's file as text and raises `SettlementMissingError` if none exists; `write` stores a file (the simulation's stand-in for the provider delivering it).

```python
    def write(self, business_date: date, text: str) -> None:
        """Store a file (the simulation's stand-in for the provider delivering it)."""
        ...
```

The writing half of the same port.

```python
@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One stored posting, as read back."""

    entry_leg: Leg
    account: Account
    amount: Money
```

`LedgerRow` is one stored posting as read back: which leg of an entry it belongs to, the account and the signed amount.

```python
@dataclass(frozen=True, slots=True)
class RefundRecord:
    """A refund recorded in the ledger."""

    refund_id: UUID
    payment_id: UUID
    amount: Money
    reference: str
    created_at: datetime
```

A refund recorded in the ledger: its id, the payment, the amount, a reference that makes it unique and the time.

```python
class PaymentRepository(Protocol):
    """Payments, looked up by the provider's transaction id."""

    def add(self, payment: LedgerPayment, idempotency_key: str | None = None) -> None:
        """Insert a payment; a repeated idempotency key violates a unique constraint."""
        ...

    def by_key(self, idempotency_key: str) -> LedgerPayment | None:
        """The payment recorded for a capture request key, if any."""
        ...
```

The payments store, looked up by the provider's transaction id. `add` inserts a payment (a repeated idempotency key violates a unique constraint); `by_key` finds the payment recorded for a capture request key.

```python
    def lock_txn(self, provider_txn_id: str) -> None:
        """Serialize resolvers of one provider transaction until the unit of work ends."""
        ...
```

`lock_txn` serializes resolvers of one provider transaction until the unit of work ends: two people or processes fixing the same transaction take turns.

```python
    def for_txn(self, provider_txn_id: str) -> list[LedgerPayment]:
        """Every payment (active or reversed) carrying this provider transaction id, oldest first."""
        ...
```

Every payment, active or reversed, carrying one provider id, oldest first.

```python
    def captured_on(self, day: date) -> list[LedgerPayment]:
        """Payments captured on a UTC calendar day, any state."""
        ...
```

Payments captured on a UTC calendar day in any state.

```python
    def by_txns(self, provider_txn_ids: Collection[str]) -> list[LedgerPayment]:
        """Payments for a set of provider transaction ids."""
        ...
```

Payments for a set of provider ids.

```python
    def save(self, payment: LedgerPayment) -> None:
        """Write the changed state, fee and refunded total of an existing payment."""
        ...
```

Writes the changed state, fee and refunded total of an existing payment.

```python
class LedgerRepository(Protocol):
    """The append-only ledger."""

    def post(
        self, payment_id: UUID, operation: str, reference: str, entries: Sequence[JournalEntry], at: datetime
    ) -> None:
        """Append whole entries. The store refuses a repeat of the same (payment, operation, reference, leg)."""
        ...
```

The append-only ledger. `post` appends whole entries and the store refuses a repeat of the same (payment, operation, reference, leg).

```python
    def rows_for(self, payment_id: UUID) -> list[LedgerRow]:
        """Every posting of one payment, in the order posted."""
        ...
```

Every posting of one payment in the order posted.

```python
    def total(self, currency: str) -> Money:
        """The sum of every posting; zero whenever the ledger is whole."""
        ...
```

The sum of every posting, which is zero whenever the ledger is whole.

```python
    def digest(self) -> str:
        """A SHA-256 over every posting in order, to show that a second pass changed nothing."""
        ...
```

A SHA-256 fingerprint over every posting in order. If a second pass of the resolvers leaves the digest unchanged, it changed nothing.

```python
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
```

Refunds recorded in the ledger: `add`, `exists` (does this payment already have a refund with this reference?) and `recorded_on` (refunded totals by provider id for one UTC day, as a dictionary).

```python
class BreakStatus(StrEnum):
    """Where a break is in its life."""

    OPEN = "OPEN"  # found, a rule can resolve it
    CARRIED = "CARRIED"  # a timing difference waiting for the next report
    AUTO_RESOLVED = "AUTO_RESOLVED"  # cleared by a rule from the report's own number (fees)
    RESOLVED = "RESOLVED"  # fixed in the ledger from the report's data
    NEEDS_REVIEW = "NEEDS_REVIEW"  # nothing can decide: on the worklist
    CLEARED = "CLEARED"  # went away by itself (a timing difference that settled, a break that no longer reproduces)
    ESCALATED = "ESCALATED"  # a timing difference that waited too long; a new break holds it now
```

Where a break is in its life: `OPEN` (found; a rule can resolve it), `CARRIED` (a timing difference waiting for the next report), `AUTO_RESOLVED` (cleared by a rule from the report's own number, the fee case), `RESOLVED` (fixed in the ledger from the report's data), `NEEDS_REVIEW` (nothing can decide; it is on the worklist), `CLEARED` (went away by itself) and `ESCALATED` (a timing difference that waited too long; a new break holds it now).

```python
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
```

A break as persisted: the finding (a `Break` from the domain) plus an id, the run date, the status, a note, the time of the last change and, if it cleared, the date it did. The last field has a default (`= None`), so it may be left out when creating one.

```python
class BreakRepository(Protocol):
    """Breaks found by reconciliation runs."""

    def find(self, run_date: date, provider_txn_id: str, kind: SettlementKind, cause: str) -> StoredBreak | None:
        """The stored break with this natural key, if any."""
        ...

    def insert(self, run_date: date, finding: Break, status: BreakStatus, note: str, at: datetime) -> StoredBreak:
        """Store a newly found break."""
        ...
```

Breaks found by reconciliation runs. `find` looks a break up by its natural key (run date, transaction id, kind, cause); `insert` stores a new one and returns it.

```python
    def get_for_update(self, break_id: int) -> StoredBreak | None:
        """Read and lock one break until the unit of work ends."""
        ...
```

Reads and locks one break until the transaction ends.

```python
    def update(
        self, break_id: int, status: BreakStatus, note: str, at: datetime, cleared_on: date | None = None
    ) -> None:
        """Change a break's status and note."""
        ...
```

Changes a break's status and note (and optionally the date it cleared).

```python
    def refresh(self, break_id: int, finding: Break, at: datetime) -> None:
        """Replace the numbers of an unresolved break with this run's findings."""
        ...
```

Replaces the numbers of an unresolved break with this run's findings.

```python
    def for_run(self, run_date: date) -> list[StoredBreak]:
        """Every break of one run, in provider-id order."""
        ...
```

Every break of one run in provider-id order.

```python
    def carried(self) -> list[StoredBreak]:
        """Timing differences still waiting, oldest first."""
        ...
```

Timing differences still waiting, oldest first.

```python
    def cleared_on(self, day: date) -> list[StoredBreak]:
        """Carried breaks that cleared on this day's run."""
        ...
```

Carried breaks that cleared on this day's run.

```python
    def worklist(self) -> list[StoredBreak]:
        """Breaks that need a person, oldest first."""
        ...
```

Breaks that need a person, oldest first.

```python
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
```

The numbers of one reconciliation run, kept so the report can show the first pass after the fixes: how many ledger payments and settlement rows, the settled and refunded totals, the count and total of clean transactions, the number of breaks and when it ran.

```python
class RunRepository(Protocol):
    """Reconciliation runs."""

    def save(self, run: RunRecord) -> None:
        """Record the first pass of a day; a later run of the same day only counts itself."""
        ...
```

Reconciliation runs: `save` records the first pass of a day (a later run of the same day only counts itself) and `get` returns the run for a date if it ran.

```python
    def get(self, run_date: date) -> RunRecord | None:
        """The run for a date, if it ran."""
        ...
```

The second method of the run store.

```python
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
```

A **unit of work** is one database transaction seen as a single object: it commits when the block ends normally and rolls back on an exception. It exposes the five stores (`payments`, `ledger`, `refunds`, `breaks`, `runs`) as read-only properties. The use cases only ever see this shape; PostgreSQL provides it, and the unit tests provide an in-memory one.

```python
UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]
```

`UnitOfWorkFactory` is the type of a function that takes no arguments and returns something usable in a `with` block that yields a `UnitOfWork`.

```python
class HealthProbe(Protocol):
    """Reports whether a dependency is reachable."""

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        ...
```

`HealthProbe` has one method, `check()`, that returns normally when healthy and raises when not.

## Try it yourself

1. Run the unit tests of the use cases (no database needed):

   ```bash
   .venv/bin/python -m pytest tests/unit/application -q
   ```

   Expected: all pass (`NN passed`).

2. See a rendered daily report: run the whole scenario on the in-memory fakes through the tests with `-k lesson -s`, or after `make up` use `make demo`, then read the printed report.

3. See the shape of a break as JSON-ready data: read `break_view` above and compare it with the output of `GET /v1/worklist` after `make demo` while the API runs.

## Mistakes people make

- **Creating collaborators inside a use case.** Passing the clock, ids, files and provider in is what makes the code testable.
- **Resolving without a lock.** Two resolvers could both insert the missing capture; `lock_txn` and `uuid5` ids prevent it.
- **Resolving a break twice and posting twice.** Each resolver first checks whether its work is already done, and the database refuses a repeated (payment, operation, reference, leg).
- **Deleting a wrong ledger row.** The ledger is append-only; mistakes are corrected with reversals and corrections that show both.
- **Treating a timing difference as an error.** It is carried and cleared when the next report contains it.

## Check yourself

1. What does a unit of work give a use case? *One transaction with access to all the stores; it commits at the end of the block or rolls back on an exception.*
2. Why does `ReconcileDay` save the run record before the breaks? *The breaks refer to the run.*
3. What makes the resolvers safe to run twice? *They check whether the work is done, use repeatable ids, and the database refuses repeated ledger legs.*
4. In which order are the resolvers applied? *Missing captures, duplicates on our side, fee discrepancies, provider-only refunds.*
5. What does `DailyReport.balances` prove? *That the buckets add up to the settlement report's settled total.*

## Further reading

- Python 3.12 `typing.Protocol`: https://docs.python.org/3.12/library/typing.html#typing.Protocol
- Python 3.12 `uuid` (`uuid5`): https://docs.python.org/3.12/library/uuid.html
- Python 3.12 `logging`: https://docs.python.org/3.12/library/logging.html
- Python 3.12 `contextlib.AbstractContextManager`: https://docs.python.org/3.12/library/contextlib.html
