"""The database refuses what the domain never produces: an unbalanced entry, an edit to the ledger, a bad break."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import errors

from tests.conftest import SQL

pytestmark = pytest.mark.integration

NOW = "2026-10-20T10:00:00Z"


def new_payment(connection: psycopg.Connection[Any], txn: str | None = None) -> str:
    payment_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) "
        "VALUES (%s, %s, 'USD', 1000, 29, 'CAPTURED', 'SERVICE', %s)",
        (payment_id, txn or f"ptx_{uuid.uuid4().hex[:10]}", NOW),
    )
    return payment_id


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


def add_posting(connection: psycopg.Connection[Any], entry_id: str, payment_id: str, account: str, amount: int) -> None:
    connection.execute(
        "INSERT INTO postings (entry_id, payment_id, account, amount_minor, currency, created_at) "
        "VALUES (%s, %s, %s, %s, 'USD', %s)",
        (entry_id, payment_id, account, amount, NOW),
    )


def check_deferred_constraints_now(connection: psycopg.Connection[Any]) -> None:
    """Fire the checks that would run at COMMIT, then roll back so the test leaves nothing behind."""
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    connection.rollback()


def test_a_balanced_pair_passes_the_commit_check(connection):
    payment_id = new_payment(connection)
    entry_id = add_entry(connection, payment_id)
    add_posting(connection, entry_id, payment_id, "customer_receivable", 1000)
    add_posting(connection, entry_id, payment_id, "merchant_pending", -1000)
    check_deferred_constraints_now(connection)


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
def test_an_entry_that_is_not_a_balanced_pair_cannot_commit(connection, rows):
    payment_id = new_payment(connection)
    entry_id = add_entry(connection, payment_id)
    for account, amount in rows:
        add_posting(connection, entry_id, payment_id, account, amount)
    with pytest.raises(errors.CheckViolation):
        connection.commit()


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


def test_two_payments_may_carry_one_provider_id_because_that_is_the_break_to_find(connection):
    new_payment(connection, "ptx_dupe0001")
    new_payment(connection, "ptx_dupe0001")
    assert connection.execute("SELECT count(*) FROM payments WHERE provider_txn_id = 'ptx_dupe0001'").fetchone() == (2,)
    connection.rollback()


def test_an_operation_posts_once_per_payment_reference_and_leg(connection):
    payment_id = new_payment(connection)
    add_entry(connection, payment_id)
    with pytest.raises(errors.UniqueViolation, match="journal_entries_once_per_leg"):
        add_entry(connection, payment_id)


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
def test_a_payment_that_breaks_a_rule_is_refused(connection, sql, constraint):
    with pytest.raises(errors.CheckViolation, match=constraint):
        connection.execute(sql)


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


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("cause", "mystery", "reconciliation_breaks_cause_valid"),
        ("status", "FIXED", "reconciliation_breaks_status_valid"),
        ("kind", "other", "reconciliation_breaks_kind_valid"),
        ("cleared_on", "'2030-01-02'", "reconciliation_breaks_cleared_on_matches_status"),
    ],
)
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


def test_the_seed_holds_six_captured_payments_and_the_whole_ledger_sums_to_zero(connection):
    assert connection.execute("SELECT count(*) FROM payments WHERE provider_txn_id LIKE 'ptx_s%'").fetchone() == (6,)
    assert connection.execute("SELECT COALESCE(sum(amount_minor), 0) FROM postings").fetchone() == (0,)
    assert connection.execute(
        "SELECT count(*) FROM postings WHERE payment_id = 'bbbbbbbb-0000-4000-8000-000000000001'"
    ).fetchone() == (6,)


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


def test_the_migrations_build_the_same_schema_as_001_schema(settings):
    direct, migrated = catalogs(settings)
    assert len(direct) > 60
    assert migrated == direct
