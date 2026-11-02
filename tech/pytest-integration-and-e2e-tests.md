# pytest walkthrough: the integration and end-to-end tests, line by line

## What it is

This note reads every line of the tests that need the real PostgreSQL (`make up` first): the six breaks found, classified and resolved on real transactions, the schema constraints, and the end-to-end tests that run the demo program and start the actual server as separate processes.

An **integration test** checks that parts work together, here the use cases with the real database adapters. An **end-to-end test** drives the finished program from the outside.

## Why this repository uses it

Some promises can only be shown with the real system: that eight workers resolving one break post one correction (it depends on PostgreSQL's row and advisory locks), that a resolver that fails halfway leaves no trace (it depends on real transactions), that the database itself refuses a bad break, and that SIGTERM stops the server cleanly. Fakes cannot prove these.

## Where it appears

| File | What it is |
| --- | --- |
| `tests/integration/conftest.py` | Fixtures for the real database. |
| `tests/integration/test_reconciliation_on_postgres.py` | The six breaks on PostgreSQL, with races and rollbacks. |
| `tests/integration/test_schema_constraints.py` | The database refuses bad data. |
| `tests/e2e/conftest.py` | Fixtures that run the demo and start the real server. |
| `tests/e2e/test_lesson_scenario.py` | The lesson end to end. |

## How it works underneath

Each test starts from a freshly loaded schema and seed. Tests talk to PostgreSQL through psycopg. Concurrency tests start threads that wait on a `Barrier` so all start at the same instant. The end-to-end fixture runs the real command-line program once and then starts `python -m payments_reconciliation.interfaces.serve` on a free port, waits for `/health`, makes requests with httpx, and stops the process with SIGTERM.

## Walkthrough of this repository's use

Fixtures first, then the tests.

### `tests/integration/conftest.py` (65 lines)

```python
"""Fixtures for the integration tests: a pool, the real adapters, and the stub provider behind them."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import psycopg
import pytest

from payments_reconciliation.infrastructure import postgres
from payments_reconciliation.infrastructure.settings import Settings
from payments_reconciliation.infrastructure.simulation import SimulatedClock
from payments_reconciliation.infrastructure.system import RandomIds, SystemClock
from payments_reconciliation.interfaces.container import Container, assemble
from tests.conftest import load_schema, wait_until_ready
from tests.unit.fakes import START
```

Fixtures for the tests that use the real PostgreSQL: a pool, the real adapters and the stub provider behind them. `pathlib` is used for the temporary folder; the rest are as in repo 1's integration fixtures.

```python
@pytest.fixture(autouse=True)
def _schema(settings: Settings) -> None:
    """Every test starts from a freshly loaded schema and seed, so no test sees another's payments."""
    wait_until_ready(settings)
    with psycopg.connect(postgres.conninfo(settings.postgres), autocommit=True) as admin:
        load_schema(admin)
```

`@pytest.fixture(autouse=True)` above it means every test in this folder uses the fixture without asking for it. Each test starts from a freshly loaded schema and seed, so no test sees another's payments: it waits until the database is ready, then connects as an administrator (autocommit) and runs `load_schema`.

```python
@pytest.fixture
def connection(settings: Settings) -> Iterator[psycopg.Connection[Any]]:
    """A plain connection (autocommit off) for tests that write SQL directly."""
    with psycopg.connect(postgres.conninfo(settings.postgres)) as connection:
        yield connection
```

`connection` opens a plain psycopg connection (autocommit off, so a test controls commits) to the test database using the real settings. `with ... as connection` closes it at the end; `yield connection` hands it to the test.

```python
@dataclass
class Flow:
    """The use cases on PostgreSQL, with settlement files in a temporary folder."""

    services: Container
    pool: Any
```

`Flow` bundles the use cases (`services`) and the connection pool; its docstring says the settlement files are in a temporary folder.

```python
@pytest.fixture
def flow(settings: Settings, tmp_path: pathlib.Path) -> Iterator[Flow]:
    import dataclasses

    from payments_reconciliation.infrastructure.settings import ReconciliationSettings

    rules: ReconciliationSettings = dataclasses.replace(settings.reconciliation, settlement_dir=tmp_path)
    local = dataclasses.replace(settings, reconciliation=rules)
    pool = postgres.open_pool(settings.postgres)
    ids = RandomIds()
    services = assemble(
        settings=local,
        uow_factory=postgres.PostgresUnitOfWorkFactory(pool, ids),
        clock=SystemClock(),
        simulation_clock=SimulatedClock(START),
        ids=ids,
        probes={"database": postgres.PostgresHealth(pool)},
        close=pool.close,
    )
    yield Flow(services, pool)
    pool.close()
```

The `flow` fixture builds the whole application on PostgreSQL. `dataclasses.replace` copies the settings with the settlement folder replaced by the test's temporary folder (so tests never touch `var/settlement`). It then opens a pool, makes random ids, and calls `assemble(...)` with the PostgreSQL unit-of-work factory, the real system clock, a simulation clock starting at `START`, a database health probe and `pool.close` as the shutdown action. After the test (`yield Flow(...)` gives control to the test; the line after the yield runs when it ends) the pool is closed.

### `tests/integration/test_reconciliation_on_postgres.py` (146 lines)

```python
"""The lesson's six breaks, found, classified and resolved against the real database."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from payments_reconciliation.application.ports import BreakStatus
from payments_reconciliation.application.resolve_breaks import Outcome
from payments_reconciliation.domain.reconciliation import Cause
from payments_reconciliation.infrastructure.simulation import DayPlan, Plants
from tests.integration.conftest import Flow
```

The lesson's six breaks, found, classified and resolved against the real database. `threading.Barrier` releases threads together and `ThreadPoolExecutor` runs functions on a pool of threads.

```python
pytestmark = pytest.mark.integration

# The same six breaks as the lesson, at positions a 60-payment day reaches.
```

Marks every test in the file as `integration`; the Makefile selects them with `-m integration`.

```python
PLANTS = Plants(timing=5, fee=8, missing_capture=12, duplicate=15, refund=20, amount_mismatch=25)
SIX = {
    "ptx_0005": Cause.TIMING,
    "ptx_0008": Cause.FEE_DISCREPANCY,
    "ptx_0012": Cause.MISSING_CAPTURE,
    "ptx_0015": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0020": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0025": Cause.AMOUNT_MISMATCH,
}
DAY = date(2026, 10, 20)
NEXT = date(2026, 10, 21)
```

The same six breaks as the lesson, at positions a 60-payment day reaches, with the expected cause of each, and the two days used.

```python
def planted(flow: Flow, day: date = DAY) -> None:
    flow.services.simulator.run(DayPlan(day, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
```

Helper: simulate the planted 60-payment day (or another day) through the real path.

```python
def digest(flow: Flow) -> tuple[str, str]:
    with flow.services.uow_factory() as uow:
        return uow.ledger.digest(), uow.ledger.total("USD").format()
```

Helper: the ledger digest and formatted sum, read in one transaction.

```python
def test_a_clean_day_reconciles_with_zero_breaks(flow):
    flow.services.simulator.run(DayPlan(date(2026, 9, 1), 60, 42, "ptx_c"))
    outcome = flow.services.reconcile.execute(date(2026, 9, 1))
    assert outcome.breaks == [] and outcome.run.clean_transactions == 60
```

60 payments on a day in September: no breaks and 60 clean.

```python
def test_the_six_planted_breaks_are_found_and_classified_by_cause(flow):
    planted(flow)
    outcome = flow.services.reconcile.execute(DAY)
    assert {b.finding.provider_txn_id: b.finding.cause for b in outcome.breaks} == SIX
    assert outcome.run.clean_transactions == 54
```

Exactly the six causes; 54 clean.

```python
def test_resolving_twice_changes_nothing_the_second_time_and_the_ledger_stays_balanced(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    first = {r.stored.finding.provider_txn_id: r.outcome for r in flow.services.resolve.execute(DAY)}
    assert first["ptx_0008"] is first["ptx_0012"] is first["ptx_0015"] is first["ptx_0020"] is Outcome.RESOLVED
    assert first["ptx_0005"] is Outcome.CARRIED and first["ptx_0025"] is Outcome.NEEDS_REVIEW
    before = digest(flow)
    second = {r.stored.finding.provider_txn_id: r.outcome for r in flow.services.resolve.execute(DAY)}
    assert digest(flow) == before and before[1] == "0.00"
    assert second["ptx_0008"] is Outcome.ALREADY_RESOLVED and second["ptx_0020"] is Outcome.ALREADY_RESOLVED
```

The first resolve gives `RESOLVED` for the four fixable breaks, `CARRIED` for the timing and `NEEDS_REVIEW` for the amount. The digest is the same after the second resolve, the ledger sum is `0.00`, and the fixable breaks now report `ALREADY_RESOLVED`.

```python
def test_the_report_balances_and_only_the_amount_mismatch_is_left_for_a_person(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    flow.services.resolve.execute(DAY)
    report = flow.services.report.execute(DAY)
    assert report.balances and [line.provider_txn_id for line in report.worklist] == ["ptx_0025"]
    assert [b.finding.provider_txn_id for b in flow.services.worklist.execute()] == ["ptx_0025"]
```

The report balances; the worklist (in the report and from the worklist use case) holds `ptx_0025` only.

```python
def test_the_carried_timing_difference_clears_on_the_next_day_with_no_action(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    flow.services.resolve.execute(DAY)
    flow.services.simulator.run(DayPlan(NEXT, 10, 44, "ptx_n"))
    outcome = flow.services.reconcile.execute(NEXT)
    assert [b.finding.provider_txn_id for b in outcome.cleared_carried] == ["ptx_0005"] and outcome.breaks == []
```

After the next day's simulation, `ptx_0005` appears in the cleared list and the day has no breaks.

```python
def test_reconciling_a_day_twice_updates_the_same_break_rows(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    flow.services.reconcile.execute(DAY)
    with flow.services.uow_factory() as uow:
        assert len(uow.breaks.for_run(DAY)) == 6
```

Two reconciles of one day leave six break rows, not twelve.

```python
def test_eight_workers_resolving_one_break_post_one_correction(flow):
    planted(flow)
    flow.services.reconcile.execute(DAY)
    with flow.services.uow_factory() as uow:
        [fee_break] = [b for b in uow.breaks.for_run(DAY) if b.finding.cause is Cause.FEE_DISCREPANCY]
        rows_before = len(uow.ledger.rows_for(uow.payments.for_txn("ptx_0008")[0].id))
    barrier = threading.Barrier(8)

    def work() -> Outcome:
        barrier.wait(timeout=30)
        outcome: Outcome = flow.services.resolve.resolve(fee_break.id).outcome
        return outcome

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = [future.result(timeout=120) for future in [pool.submit(work) for _ in range(8)]]
    assert sorted(o.value for o in outcomes) == ["already resolved"] * 7 + ["resolved"]
    with flow.services.uow_factory() as uow:
        assert len(uow.ledger.rows_for(uow.payments.for_txn("ptx_0008")[0].id)) == rows_before + 2
    assert digest(flow)[1] == "0.00"
```

Eight threads released together resolve the same fee break. The outcomes are seven `already resolved` and one `resolved` (sorted texts compared with a list multiplication `["already resolved"] * 7 + ["resolved"]`), the payment gained exactly two ledger rows (one correction entry) and the ledger still sums to zero. This works because of the break row lock and the advisory lock.

```python
def test_a_resolver_that_fails_midway_leaves_neither_the_postings_nor_the_status(flow, monkeypatch):
    from payments_reconciliation.infrastructure.postgres import PostgresPayments

    planted(flow)
    flow.services.reconcile.execute(DAY)
    with flow.services.uow_factory() as uow:
        [fee_break] = [b for b in uow.breaks.for_run(DAY) if b.finding.cause is Cause.FEE_DISCREPANCY]
    before = digest(flow)

    def explode(self, payment):
        raise RuntimeError("connection reset")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresPayments, "save", explode)
        with pytest.raises(RuntimeError):
            flow.services.resolve.resolve(fee_break.id)
    assert digest(flow) == before
    with flow.services.uow_factory() as uow:
        stored = uow.breaks.get_for_update(fee_break.id)
        assert stored is not None and stored.status is BreakStatus.OPEN
    assert flow.services.resolve.resolve(fee_break.id).outcome is Outcome.RESOLVED
```

`PostgresPayments.save` is replaced by a function that raises, for the duration of a `monkeypatch.context()` block. The resolve raises `RuntimeError`; afterwards the ledger digest is unchanged and the break is still `OPEN` (the correction posting and the status change rolled back together). Resolving again, now unpatched, succeeds.

```python
def test_a_process_that_dies_after_the_provider_captured_leaves_no_row_and_the_provider_has_it(flow):
    result = flow.services.simulator.run(DayPlan(DAY, 60, 43, "ptx_", plant_breaks=True, plants=PLANTS))
    assert result.crashed_after_provider == ["ptx_0012"]
    with flow.services.uow_factory() as uow:
        assert uow.payments.for_txn("ptx_0012") == []
    assert flow.services.provider.executed["capture"] == 60
```

The simulated crash at `ptx_0012` leaves no payment for it in the ledger while the provider captured 60 times.

```python
def test_the_health_probe_reports_the_database(flow):
    assert flow.services.health.execute() == {"database": "ok"}
```

The health use case returns `{"database": "ok"}`.

### `tests/integration/test_schema_constraints.py` (266 lines)

```python
"""The database refuses what the domain never produces: an unbalanced entry, an edit to the ledger, a bad break."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from tests.conftest import SQL
```

The database refuses what the domain never produces: an unbalanced entry, an edit to the ledger, a bad break. `psycopg.errors` has one exception class per PostgreSQL error kind; `SQL` is the path of the `sql` folder.

```python
pytestmark = pytest.mark.integration
```

Marks every test here as `integration`.

```python
NOW = "2026-10-20T10:00:00Z"
```

A fixed timestamp text used for `created_at`.

```python
def new_payment(connection: psycopg.Connection[Any], txn: str | None = None) -> str:
    payment_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
        "VALUES (%s, %s, 'USD', 1000, 29, 'CAPTURED', 'SERVICE', %s)",
        (payment_id, txn or f"ptx_{uuid.uuid4().hex[:10]}", NOW),
    )
    return payment_id
```

Inserts a captured payment of 10.00 with a 0.29 fee, a random id and a provider id (random unless given), and returns its id. `%s` marks a value placeholder: psycopg sends the values separately from the SQL text. Adjacent strings in parentheses are joined into one SQL text.

```python
def add_entry(
    connection: psycopg.Connection[Any], payment_id: str, leg: str = "CAPTURE_GROSS", ref: str = "capture"
) -> str:
    entry_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO journal_entries (id, payment_id, operation, reference, leg, created_at) "
        "VALUES (%s, %s, 'CAPTURE', %s, %s, %s)",
        (entry_id, payment_id, ref, leg, NOW),
    )
    return entry_id
```

Inserts one `CAPTURE` journal entry with the given leg and reference and returns its id.

```python
def add_posting(connection: psycopg.Connection[Any], entry_id: str, payment_id: str, account: str, amount: int) -> None:
    connection.execute(
        "INSERT INTO postings (entry_id, payment_id, account, amount_minor, currency, created_at) "
        "VALUES (%s, %s, %s, %s, 'USD', %s)",
        (entry_id, payment_id, account, amount, NOW),
    )
```

Inserts one posting row for an entry.

```python
def check_deferred_constraints_now(connection: psycopg.Connection[Any]) -> None:
    """Fire the checks that would run at COMMIT, then roll back so the test leaves nothing behind."""
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.rollback()
```

`SET CONSTRAINTS ALL IMMEDIATE` runs every deferred check right now, instead of at commit; then `rollback()` removes the test's rows.

```python
def test_a_balanced_pair_passes_the_commit_check(connection):
    payment_id = new_payment(connection)
    entry_id = add_entry(connection, payment_id)
    add_posting(connection, entry_id, payment_id, "customer_receivable", 1000)
    add_posting(connection, entry_id, payment_id, "merchant_pending", -1000)
    check_deferred_constraints_now(connection)
```

A debit of 1000 and a credit of -1000 on different accounts passes.

```python
@pytest.mark.parametrize(
    "rows",
    [
        [("customer_receivable", 1000)],
        [("customer_receivable", 1000), ("merchant_pending", -900)],
        [("customer_receivable", 1000), ("customer_receivable", -1000)],
        [("customer_receivable", 1000), ("merchant_pending", -500), ("merchant_cash", -500)],
        [],
    ],
)
```

Five bad entries: a single row, a sum that is off by 100, both rows on one account, three rows, and no rows at all.

```python
def test_an_entry_that_is_not_a_balanced_pair_cannot_commit(connection, rows):
    payment_id = new_payment(connection)
    entry_id = add_entry(connection, payment_id)
    for account, amount in rows:
        add_posting(connection, entry_id, payment_id, account, amount)
    with pytest.raises(errors.CheckViolation):
        connection.commit()
```

Each case inserts its rows, and `connection.commit()` raises `CheckViolation` because the deferred trigger fires at commit.

```python
def test_the_ledger_cannot_be_edited_or_deleted(connection):
    for statement in (
        "UPDATE postings SET amount_minor = amount_minor + 1",
        "DELETE FROM postings",
        "DELETE FROM journal_entries",
        "UPDATE journal_entries SET leg = 'CAPTURE_FEE'",
    ):
        with pytest.raises(errors.ObjectNotInPrerequisiteState, match="append-only"):
            connection.execute(statement)
        connection.rollback()
```

Four statements (update postings, delete postings, delete entries, update entries) each raise `ObjectNotInPrerequisiteState` with the message `append-only`; the loop rolls back after each failed statement.

```python
def test_two_payments_may_carry_one_provider_id_because_that_is_the_break_to_find(connection):
    new_payment(connection, "ptx_dupe0001")
    new_payment(connection, "ptx_dupe0001")
    assert connection.execute("SELECT count(*) FROM payments WHERE provider_txn_id = 'ptx_dupe0001'").fetchone() == (2,)
    connection.rollback()
```

Two payments with `ptx_dupe0001` can be inserted, and a count shows two. Here, unlike the payments service, the duplicate is allowed on purpose.

```python
def test_an_operation_posts_once_per_payment_reference_and_leg(connection):
    payment_id = new_payment(connection)
    add_entry(connection, payment_id)
    with pytest.raises(errors.UniqueViolation, match="journal_entries_once_per_leg"):
        add_entry(connection, payment_id)
```

A second entry for the same payment, operation, reference and leg violates the unique key `journal_entries_once_per_leg`.

```python
@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
            "VALUES (gen_random_uuid(), 'order-9', 'USD', 100, 3, 'CAPTURED', 'SERVICE', now())",
            "payments_txn_id_format",
        ),
        (
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
            "VALUES (gen_random_uuid(), 'ptx_1', 'USD', 100, 101, 'CAPTURED', 'SERVICE', now())",
            "payments_fee_bounds",
        ),
        (
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
            "VALUES (gen_random_uuid(), 'ptx_1', 'USD', 0, 0, 'CAPTURED', 'SERVICE', now())",
            "payments_",
        ),
        (
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
            "VALUES (gen_random_uuid(), 'ptx_1', 'USD', 100, 3, 'LOST', 'SERVICE', now())",
            "payments_state_valid",
        ),
        (
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
            "VALUES (gen_random_uuid(), 'ptx_1', 'USD', 100, 3, 'CAPTURED', 'MAGIC', now())",
            "payments_source_valid",
        ),
        (
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, "
            "refunded_minor, "
            "captured_at) VALUES (gen_random_uuid(), 'ptx_1', 'USD', 100, 3, 'CAPTURED', 'SERVICE', 101, now())",
            "payments_refund_bounds",
        ),
    ],
)
```

Six pairs of (bad INSERT, the constraint that must refuse it): a provider id of the wrong shape, a fee above the amount, a zero amount, an unknown state, an unknown source and a refund above the amount. `gen_random_uuid()` makes ids inside PostgreSQL; `now()` is the current time.

```python
def test_a_payment_that_breaks_a_rule_is_refused(connection, sql, constraint):
    with pytest.raises(errors.CheckViolation, match=constraint):
        connection.execute(sql)
```

Each statement raises `CheckViolation` whose message matches the expected constraint name.

```python
def test_a_break_needs_its_run_and_one_row_per_day_transaction_kind_and_cause(connection):
    insert = (
        "INSERT INTO reconciliation_breaks (run_date, provider_txn_id, kind, cause, status, currency, detail, note, "
        "created_at, updated_at) VALUES ('2030-01-01', 'ptx_1', 'settled', 'timing', 'CARRIED', 'USD', 'd', 'n', "
        "now(), now())"
    )
    with pytest.raises(errors.ForeignKeyViolation):
        connection.execute(insert)
    connection.rollback()
    connection.execute(
        "INSERT INTO reconciliation_runs (run_date, currency, ledger_payments, settlement_rows, settled_total_minor, "
        "refunded_total_minor, clean_transactions, clean_total_minor, break_count, ran_at, last_ran_at) "
        "VALUES ('2030-01-01', 'USD', 0, 0, 0, 0, 0, 0, 0, now(), now())"
    )
    connection.execute(insert)
    with pytest.raises(errors.UniqueViolation, match="reconciliation_breaks_natural_key"):
        connection.execute(insert)
    connection.rollback()
```

A break for a run date that has no run violates the foreign key; after inserting the run, the break can be inserted once and a second identical insert violates `reconciliation_breaks_natural_key`.

```python
@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("cause", "mystery", "reconciliation_breaks_cause_valid"),
        ("status", "FIXED", "reconciliation_breaks_status_valid"),
        ("kind", "other", "reconciliation_breaks_kind_valid"),
        ("cleared_on", "'2030-01-02'", "reconciliation_breaks_cleared_on_matches_status"),
    ],
)
```

Four bad values for a break: an unknown cause, an unknown status, an unknown kind, and a clearing date on a break whose status is not `CLEARED`.

```python
def test_a_break_with_an_unknown_cause_status_or_kind_is_refused(connection, column, value, constraint):
    connection.execute(
        "INSERT INTO reconciliation_runs (run_date, currency, ledger_payments, settlement_rows, settled_total_minor, "
        "refunded_total_minor, clean_transactions, clean_total_minor, break_count, ran_at, last_ran_at) "
        "VALUES ('2030-01-01', 'USD', 0, 0, 0, 0, 0, 0, 0, now(), now()) ON CONFLICT DO NOTHING"
    )
    values = {"kind": "'settled'", "cause": "'timing'", "status": "'CARRIED'", "cleared_on": "NULL"}
    values[column] = value if value.startswith("'") else f"'{value}'"
    with pytest.raises(errors.CheckViolation, match=constraint):
        connection.execute(
            "INSERT INTO reconciliation_breaks (run_date, provider_txn_id, kind, cause, status, currency, detail, "
            f"note, cleared_on, created_at, updated_at) VALUES ('2030-01-01', 'ptx_1', {values['kind']}, "
            f"{values['cause']}, "
            f"{values['status']}, 'USD', 'd', 'n', {values['cleared_on']}, now(), now())"
        )
    connection.rollback()
```

Inserts the run (`ON CONFLICT DO NOTHING` ignores it if it exists) and then a break whose one column is replaced by the bad value (`values[column] = ...` changes one entry of the dictionary of SQL fragments). The insert raises `CheckViolation` naming the constraint.

```python
def test_the_seed_holds_six_captured_payments_and_the_whole_ledger_sums_to_zero(connection):
    assert connection.execute("SELECT count(*) FROM payments WHERE provider_txn_id LIKE 'ptx_s%'").fetchone() == (6,)
    assert connection.execute("SELECT COALESCE(sum(amount_minor), 0) FROM postings").fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM postings WHERE payment_id = 'bbbbbbbb-0000-4000-8000-000000000001'"
    ).fetchone() == (6,)
```

The seed has six payments whose ids start with `ptx_s`, the whole ledger sums to zero, and the first payment has six postings.

```python
def test_the_queries_file_runs_and_its_invariant_queries_return_nothing(connection):
    script = "\n".join(line for line in (SQL / "003_queries.sql").read_text().splitlines() if not line.startswith("\\"))
    cursor = connection.execute(script)
    results = []
    while True:
        results.append(cursor.fetchall() if cursor.description else None)
        if not cursor.nextset():
            break
    assert len(results) == 11
    assert results[1] == [(0,)]  # the ledger sum
    assert results[2] == []  # entries that are not balanced pairs
    assert results[4] == []  # active duplicates (the seed has none)
    assert results[5] == []  # payments without their capture rows
    connection.rollback()
```

Runs `003_queries.sql` after dropping the `psql` meta-command lines (those starting with a backslash). The results are read one after another with `nextset()`. There are eleven results (query 2 has two statements); the ledger sum is 0 and the unbalanced-entry, active-duplicate and missing-rows queries return nothing.

```python
def catalogs(settings: Any) -> tuple[list[Any], list[Any]]:
    from payments_reconciliation.infrastructure.postgres import conninfo

    catalog_sql = """
        SELECT 'column', c.table_name || '.' || c.column_name,
               c.data_type || ' ' || c.is_nullable || ' ' || COALESCE(c.column_default, '')
          FROM information_schema.columns c WHERE c.table_schema = 'public'
        UNION ALL
        SELECT 'constraint', conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid) FROM pg_constraint
         WHERE connamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'index', tablename || '.' || indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'
        UNION ALL
        SELECT 'trigger', tgrelid::regclass::text || '.' || tgname,
               regexp_replace(pg_get_triggerdef(oid), ' ON .*? ', ' ON ')
          FROM pg_trigger WHERE NOT tgisinternal
        UNION ALL
        SELECT 'function', proname, prosrc FROM pg_proc WHERE pronamespace = 'public'::regnamespace
        UNION ALL
        SELECT 'comment', c.relname, obj_description(c.oid) FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
        ORDER BY 1, 2
    """
    found = []
    suffix = uuid.uuid4().hex[:8]
    with psycopg.connect(conninfo(settings.postgres), autocommit=True) as admin:
        for label, scripts in (
            ("direct", [SQL / "001_schema.sql"]),
            ("migrated", sorted((SQL / "migrations").glob("*.sql"))),
        ):
            name = f"scratch_{label}_{suffix}"
            admin.execute(f'CREATE DATABASE "{name}"')
            try:
                info = conninfo(settings.postgres).replace(f"dbname={settings.postgres.database}", f"dbname={name}")
                with psycopg.connect(info, autocommit=True) as scratch:
                    for script in scripts:
                        scratch.execute(script.read_text())
                    found.append(scratch.execute(catalog_sql).fetchall())
            finally:
                admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    return found[0], found[1]
```

Builds two scratch databases, one from `001_schema.sql` directly and one by running every migration in order, and returns the contents of the PostgreSQL system catalogs for each. `catalog_sql` is one query joined with `UNION ALL` that lists columns, constraints, indexes, triggers, functions and table comments, sorted. For each label the helper creates a throwaway database with a random suffix, connects to it by replacing the database name in the connection string, runs the scripts and reads the catalog; `finally` drops it with `WITH (FORCE)` even if something failed.

```python
def test_the_migrations_build_the_same_schema_as_001_schema(settings):
    direct, migrated = catalogs(settings)
    assert len(direct) > 60
    assert migrated == direct
```

The two catalogs are identical, and the direct one has more than 60 entries (so the comparison is not vacuous).

### `tests/e2e/conftest.py` (86 lines)

```python
"""Fixtures for the end-to-end tests: the real command-line program and the real server process."""

from __future__ import annotations

import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx
import pytest
```

Fixtures for the end-to-end tests: the real command-line program and the real server process. New imports: `signal`, `socket`, `subprocess`, `sys`, `time`, `pathlib` and `httpx`.

```python
@pytest.fixture(autouse=True)
def _schema(database: None) -> None:
    """Every test in this folder starts from the loaded schema (the module fixture below runs the scenario once)."""
```

`@pytest.fixture(autouse=True)` above it: every test in this folder starts from the loaded schema (the module fixture below runs the scenario once).

```python
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
```

Finds an unused port: bind to port 0 (the operating system picks a free one), read it back with `getsockname()` and close.

```python
@dataclass
class Server:
    process: subprocess.Popen[str]
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
```

A running server: the process handle and its port; the property `url` builds the address.

```python
def start_server(env: dict[str, str]) -> Server:
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "payments_reconciliation.interfaces.serve"],
        env={**env, "API_HOST": "127.0.0.1", "API_PORT": str(port), "LOG_LEVEL": "INFO"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"the server exited early:\n{process.stdout.read() if process.stdout else ''}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                return Server(process, port)
        except httpx.HTTPError:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError("the server did not become healthy in 60 seconds")
```

Starts `python -m payments_reconciliation.interfaces.serve` with the given environment plus the host, the free port and the log level; its output is captured (`stdout=PIPE`, errors merged in by `stderr=STDOUT`). Then it waits up to 60 seconds (`time.monotonic()`) for `/health` to return 200. If the process exits early the error includes its output; if the deadline passes the process is killed.

```python
def stop_server(server: Server) -> tuple[int, str]:
    server.process.send_signal(signal.SIGTERM)
    try:
        output, _ = server.process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        server.process.kill()
        output, _ = server.process.communicate()
    return server.process.returncode, output
```

Sends `SIGTERM`, waits up to 30 seconds for the process to end and reads its output; if it hangs it is killed. Returns the exit code and the log text.

```python
@pytest.fixture(scope="module")
def scenario(
    tmp_path_factory: pytest.TempPathFactory, database: None
) -> tuple[dict[str, str], subprocess.CompletedProcess[str]]:
    """Run the whole lesson scenario once (the `demo` program) into a temporary settlement folder."""
    folder: pathlib.Path = tmp_path_factory.mktemp("settlement")
    env = {**os.environ, "SETTLEMENT_DIR": str(folder)}
    completed = subprocess.run(
        [sys.executable, "-m", "payments_reconciliation.interfaces.cli", "demo"],
        env=env,
        capture_output=True,
        text=True,
        timeout=3000,
        check=False,
    )
    return env, completed
```

A module-scoped fixture (`@pytest.fixture(scope="module")` above it): it runs the whole lesson scenario **once** by starting the command-line program `python -m payments_reconciliation.interfaces.cli demo` as a subprocess, with `SETTLEMENT_DIR` pointing to a fresh temporary folder (`tmp_path_factory.mktemp`), capturing its output, with a timeout of 3000 seconds and `check=False` so a failure does not raise by itself. It returns the environment (for the server test to reuse) and the completed process.

### `tests/e2e/test_lesson_scenario.py` (61 lines)

```python
"""The lesson's scenario through the real command-line program and a real server process, failures included."""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import start_server, stop_server
```

The lesson's scenario through the real command-line program and a real server process, failures included.

```python
pytestmark = pytest.mark.e2e
```

Marks every test in the file as `e2e`.

```python
def test_the_demo_program_runs_the_whole_scenario_and_exits_zero(scenario):
    _, completed = scenario
    assert completed.returncode == 0, completed.stdout + completed.stderr
    out = completed.stdout
    for line in (
        "0 breaks found",  # the clean day
        "breaks found: 6",
        "  ptx_0050  timing",
        "  ptx_0112  fee_discrepancy",
        "  ptx_0200  missing_capture",
        "  ptx_0215  duplicate_on_our_side",
        "  ptx_0300  provider_only_refund",
        "  ptx_0400  amount_mismatch",
        "ptx_0112: fee corrected, +0.18 posted, ledger balanced",
        "ptx_0200: inserted from settlement report, postings added, ledger balanced",
        "ptx_0215: duplicate reversed, one capture retained, ledger balanced",
        "ptx_0300: refund posted from settlement report, ledger balanced",
        "ptx_0112: already resolved",
        "Matched at the first pass: 494 transactions",
        "[balances]",
        "cleared carried differences: ptx_0050",
        "ledger sum across every posting: 0.00",
    ):
        assert line in out, line
    assert "FAILED" not in out and "Traceback" not in completed.stderr
```

The demo exits with status 0, and its output contains each of the lines of the lesson: the clean day with no breaks, the six breaks by cause, each fix message, the second resolve saying `already resolved`, 494 matched at the first pass, `[balances]`, the cleared carried `ptx_0050` and a zero ledger sum. The output must contain neither `FAILED` nor a Python traceback.

```python
def test_the_server_serves_the_report_and_the_worklist_of_the_planted_day(scenario):
    env, completed = scenario
    assert completed.returncode == 0
    server = start_server(env)
    try:
        report = httpx.get(f"{server.url}/v1/days/2026-10-20/report", timeout=30).json()
        assert report["balances"] is True and report["clean_transactions"] == 494
        assert [line["provider_txn_id"] for line in report["worklist"]] == ["ptx_0400"]
        assert [line["provider_txn_id"] for line in report["resolved"]] == ["ptx_0200", "ptx_0215", "ptx_0300"]
        worklist = httpx.get(f"{server.url}/v1/worklist", timeout=30).json()["worklist"]
        assert [item["provider_txn_id"] for item in worklist] == ["ptx_0400"]
        reconcile = httpx.post(f"{server.url}/v1/days/2026-10-20/reconcile", timeout=60).json()
        assert reconcile["run"]["break_count"] == 1 and reconcile["run"]["clean_transactions"] == 498
        assert [item["provider_txn_id"] for item in reconcile["breaks"] if item["status"] == "NEEDS_REVIEW"] == [
            "ptx_0400"
        ]
        assert httpx.get(f"{server.url}/health", timeout=10).json() == {"status": "ok", "checks": {"database": "ok"}}
        assert httpx.post(f"{server.url}/v1/days/2020-01-01/reconcile", timeout=30).status_code == 404
    finally:
        code, output = stop_server(server)
    assert code == 0, output
    assert "shutdown complete" in output and "level=ERROR" not in output
```

Starts a real server on the same settlement folder and asks it: the report of the planted day balances with 494 clean transactions; its worklist holds `ptx_0400` and its resolved section holds the three report-resolved ids; `/v1/worklist` agrees; reconciling again over HTTP gives one break and 498 clean; `/health` reports the database; an unknown day is a 404. The `finally` block stops the server with SIGTERM and then the test requires exit code 0 and the log line `shutdown complete` without any `level=ERROR`.

## Try it yourself

1. Start the database, then run the layers (each is a separate make target):

   ```bash
   make up
   make test-integration
   make test-e2e
   make down
   ```

   Expected: all integration tests pass, then all end-to-end tests pass.

2. Watch a constraint refuse bad data: run `make db`, then in a `psql` session try an `UPDATE postings SET amount_minor = 1 WHERE id = 1` and compare the error with `test_the_ledger_cannot_be_edited_or_deleted`.

## Mistakes people make

- **Tests that leave data behind.** Every test reloads the schema; the constraint tests roll back.
- **Races that only sometimes show.** Starting threads on a barrier gives them the same instant.
- **Fixed ports.** The e2e fixture asks the operating system for a free port.
- **Waiting without a limit.** Every wait here has a timeout.

## Check yourself

1. What does a `Barrier(8)` do? *Makes 8 threads wait until all 8 have arrived, then releases them together.*
2. Why does the unbalanced-entry test call `commit()`? *The balance check is deferred to commit time.*
3. How does the e2e fixture know the server is ready? *It polls `/health` until it returns 200.*
4. Why compare two scratch databases? *To prove the migrations build exactly the same schema as the direct schema file.*
5. What exit code must the server give after SIGTERM? *0, after logging `shutdown complete`.*

## Further reading

- pytest documentation: https://docs.pytest.org/en/stable/
- pytest fixtures: https://docs.pytest.org/en/stable/how-to/fixtures.html
- pytest parametrize: https://docs.pytest.org/en/stable/how-to/parametrize.html
- Python 3.12 `threading`: https://docs.python.org/3.12/library/threading.html
- psycopg 3 documentation: https://www.psycopg.org/psycopg3/docs/
