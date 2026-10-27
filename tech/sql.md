# SQL

## What it is

SQL (Structured Query Language) is the language for talking to a relational database. A **table** has named columns and rows. SQL has two jobs here:

- **DDL** (data definition language): `CREATE TABLE`, `ALTER TABLE`, `CREATE INDEX`, `CREATE FUNCTION`, `CREATE TRIGGER`. It describes the structure and the rules.
- **DML** (data manipulation language) and queries: `INSERT`, `UPDATE`, `DELETE`, `SELECT`. They change and read rows.

This repository uses the PostgreSQL dialect (see `postgresql.md` for the server itself).

## Why this repository uses it

The lesson asks for SQL files that hold the ledger and the reconciliation results with rules the database enforces, seed the worked sample, answer the questions a reconciler asks (what does each cause count to, what is waiting, what needs a person), and show the stepwise migration path. Everything is in plain `.sql` files you can run and read.

## Where it appears

| Path | Kind | Purpose |
| --- | --- | --- |
| `sql/001_schema.sql` | DDL | The whole schema: seven tables, constraints, indexes, trigger functions. |
| `sql/002_seed.sql` | DML | The chart of accounts and a sample day (`2026-10-19`) of six captured payments with their ledger rows. |
| `sql/003_queries.sql` | Queries | Ten reporting queries, run by `make queries`. |
| `sql/reset.sql` | DDL | `DROP SCHEMA public CASCADE; CREATE SCHEMA public;` |
| `sql/migrations/001_payments_and_ledger.sql`, `002_reconciliation.sql`, `003_resolutions.sql` | DDL | The same schema reached in three steps. |
| `src/.../infrastructure/postgres.py` | DML/queries | The statements the program runs. |
| `tests/integration/test_schema_constraints.py` | DML | Statements that try to break the rules. |

## How it works underneath

The client sends SQL text. The server **parses** it, **plans** how to run it (which indexes to use, in which order to join tables) and **executes** the plan. Statements end with `;`. `--` starts a comment to the end of the line. Text values use single quotes (`'USD'`); to put a quote inside, double it (`'provider''s fee'`). Identifiers (table and column names) are lower-case by convention.

## Walkthrough of this repository's use

### Types

`TEXT` (any length string), `UUID`, `BIGINT` (64-bit integer), `INTEGER`, `CHAR(3)` (fixed three characters), `DATE`, `TIMESTAMPTZ` (an instant in time with a time zone). Money is `BIGINT` minor units, never a decimal or float.

### Tables and constraints

```sql
CREATE TABLE payments (
    id              UUID PRIMARY KEY,
    provider_txn_id TEXT        NOT NULL,
    amount_minor    BIGINT      NOT NULL,
    fee_minor       BIGINT      NOT NULL,
    state           TEXT        NOT NULL,
    refunded_minor  BIGINT      NOT NULL DEFAULT 0,
    idempotency_key TEXT UNIQUE,
    captured_at     TIMESTAMPTZ NOT NULL,
    CONSTRAINT payments_txn_id_format CHECK (provider_txn_id ~ '^ptx_[A-Za-z0-9]{1,32}$'),
    CONSTRAINT payments_amount_positive CHECK (amount_minor > 0),
    CONSTRAINT payments_fee_bounds CHECK (fee_minor BETWEEN 0 AND amount_minor),
    CONSTRAINT payments_state_valid CHECK (state IN ('CAPTURED', 'DUPLICATE_REVERSED')),
    ...
);
```

- `PRIMARY KEY`: unique and not null; identifies a row. `UNIQUE` on `idempotency_key` allows several `NULL`s (each `NULL` counts as different) but no repeated key.
- `NOT NULL`: the column must have a value. `DEFAULT 0`: the value used when an insert gives none.
- `CONSTRAINT name CHECK (condition)`: a rule every row must satisfy. `~` is "matches this regular expression": a provider transaction id must look like `ptx_` followed by 1 to 32 letters or digits. `BETWEEN a AND b` is inclusive. `IN (...)` is a list of allowed values.
- Naming constraints makes error messages and tests precise.

### References, uniqueness and the natural key

```sql
payment_id UUID NOT NULL REFERENCES payments (id),
CONSTRAINT journal_entries_once_per_leg UNIQUE (payment_id, operation, reference, leg),
CONSTRAINT reconciliation_breaks_natural_key UNIQUE (run_date, provider_txn_id, kind, cause),
run_date DATE NOT NULL REFERENCES reconciliation_runs (run_date),
```

- `REFERENCES`: the value must exist in the other table (a foreign key). You cannot post to a payment that does not exist, or store a break for a day with no run.
- `UNIQUE (a, b, c, d)`: the **combination** must be unique. For breaks this is the identity of a difference: which day, which transaction, which side (settled or refunded), which cause.

### Identity column and indexes

```sql
id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
CREATE INDEX payments_provider_txn_id_idx ON payments (provider_txn_id);
```

`GENERATED ALWAYS AS IDENTITY` makes the database number the rows 1, 2, 3 and refuse a manual value. An **index** is a sorted side structure that makes lookups by that column fast; the matcher looks payments up by provider id constantly.

### Functions and triggers (PL/pgSQL)

```sql
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; post a correcting entry instead', TG_TABLE_NAME
        USING ERRCODE = 'object_not_in_prerequisite_state';
END
$$;
```

`$$ ... $$` is a quoting style that lets the function body contain single quotes. `plpgsql` is PostgreSQL's procedural language. `RAISE EXCEPTION` aborts the statement with a message; `%` is replaced by the next argument; `TG_TABLE_NAME` is a variable the trigger machinery provides; `USING ERRCODE` sets the error class. The balance function uses `SELECT ... INTO row_count, total, ...` to put aggregate results into variables and `IF ... THEN ... END IF` to decide.

### Changing the structure in steps (migrations)

```sql
ALTER TABLE payments DROP CONSTRAINT payments_state_valid;
ALTER TABLE payments
    ADD CONSTRAINT payments_state_valid CHECK (state IN ('CAPTURED', 'DUPLICATE_REVERSED'));
```

`ALTER TABLE` changes an existing table. A `CHECK` cannot be edited, so a migration drops it and adds the wider one. Migration `003_resolutions.sql` does this to allow the new states, sources and ledger operations (fee corrections, reversals) that the resolving step introduces. Each file is wrapped in `BEGIN`/`COMMIT` so it applies completely or not at all. `make migrate` applies all three to a scratch database so a test can prove the result equals `001_schema.sql`.

### Inserting the sample day

```sql
INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, refunded_minor,
                      idempotency_key, captured_at) VALUES
    ('bbbbbbbb-0000-4000-8000-000000000001', 'ptx_s0001', 'USD', 4250, 123, 'CAPTURED', 'SERVICE', 0, 'seed-capture-0001', '2026-10-19T10:00:00Z'),
    ...
```

Six payments; the ledger rows follow. The provider's file for that day, `data/settlement_2026-10-19.csv`, agrees on everything except the fee of `ptx_s0002` (0.60 settled against 0.52 recorded), so the sample reconciles to exactly one `fee_discrepancy`.

### Queries

```sql
SELECT cause, status, count(*) AS breaks FROM reconciliation_breaks GROUP BY cause, status ORDER BY cause, status;
```

`GROUP BY` makes one output row per distinct pair; `count(*)` counts the rows in each group; `ORDER BY` sorts the result.

```sql
SELECT captured_at::date AS day,
       count(*) FILTER (WHERE state = 'CAPTURED') AS captured,
       sum(amount_minor) FILTER (WHERE state = 'CAPTURED') AS gross_minor
  FROM payments GROUP BY 1 ORDER BY 1;
```

- `captured_at::date` converts a timestamp to a calendar day (in the session's zone, which is UTC here). `GROUP BY 1` means "by the first selected column".
- `count(*) FILTER (WHERE ...)` counts only some rows of the group.

```sql
SELECT provider_txn_id, count(*) AS active_payments, array_agg(source ORDER BY captured_at) AS sources
  FROM payments WHERE state = 'CAPTURED'
 GROUP BY provider_txn_id HAVING count(*) > 1;
```

`HAVING` filters groups after `GROUP BY` (a `WHERE` filters rows before). `array_agg` collects the sources of each group into one list. This finds the **duplicate** break: more than one active payment for one provider id. After resolution it returns no rows.

```sql
SELECT provider_txn_id, run_date, (current_date - run_date) AS days_waiting
  FROM reconciliation_breaks WHERE status = 'CARRIED' ORDER BY run_date;
```

Subtracting two dates gives a number of days. `CARRIED` breaks are timing differences waiting for a later report.

### Statements the program runs

```sql
INSERT INTO reconciliation_runs (...) VALUES (...)
ON CONFLICT (run_date) DO UPDATE SET reruns = reconciliation_runs.reruns + 1, last_ran_at = excluded.last_ran_at
```

An **upsert**: insert the run; if a row with this `run_date` already exists, update it instead. On a rerun only the counter and the time change, so the first-pass numbers stay as they were. `excluded` is the row that was proposed for insertion.

```sql
SELECT ... FROM payments WHERE provider_txn_id = ANY(%s) ORDER BY provider_txn_id, captured_at, id
```

`= ANY(array)` is true when the value equals any element of the array; the program passes a Python list as the parameter.

```sql
INSERT INTO reconciliation_breaks (...) VALUES (...) RETURNING id
```

`RETURNING` gives back columns of the row just inserted, here the number the database assigned.

```sql
WHERE captured_at >= %s AND captured_at < %s
```

A UTC day is a half-open range: from midnight (included) to the next midnight (excluded), so no instant belongs to two days.

Parameters (`%s`) are always passed separately from the SQL text (see `psycopg.md`).

## Try it yourself

1. Load everything, then count rows per table:

   ```bash
   make up
   docker compose exec -T postgres psql -U reconciliation -d reconciliation -c "select (select count(*) from payments) as payments, (select count(*) from postings) as postings"
   ```

   Expected: `payments | postings` with `6 | 36`.

2. Reconcile the sample day and read the break:

   ```bash
   make reconcile-sample
   docker compose exec -T postgres psql -U reconciliation -d reconciliation -c "select provider_txn_id, cause, status, ledger_fee_minor, settlement_fee_minor from reconciliation_breaks"
   ```

   Expected: one row, `ptx_s0002 | fee_discrepancy` with fees `52` and `60`; its status after the `resolve` step in that target is `AUTO_RESOLVED`.

3. Break a rule on purpose:

   ```bash
   docker compose exec -T postgres psql -U reconciliation -d reconciliation -c "insert into payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, captured_at) values (gen_random_uuid(), 'not-an-id', 'USD', 100, 1, 'CAPTURED', 'SERVICE', now())"
   ```

   Expected: `ERROR:  new row for relation "payments" violates check constraint "payments_txn_id_format"`.

4. Apply the stepwise migrations to a scratch database: `make migrate`. Expected: `applying sql/migrations/001_payments_and_ledger.sql` ... `003_resolutions.sql`, then a table list.

## Mistakes people make

- **Forgetting `NOT NULL`**, which lets `NULL` slip past `CHECK` conditions (a check with `NULL` is "unknown", which passes).
- **`=` with `NULL`.** Use `IS NULL` / `IS NOT NULL`.
- **Using `INNER JOIN` where `LEFT JOIN` is needed**, which silently drops accounts with no postings.
- **Building SQL from strings.** Use parameters.
- **Filtering a day with `BETWEEN`.** Both ends are included, so midnight would count twice; use `>=` and `<`.
- **Editing a posted ledger row.** Here the trigger forbids it; post a correcting entry.

## Check yourself

1. What does `LEFT JOIN` keep that `JOIN` drops? *Rows of the left table with no match on the right.*
2. What is the natural key of a break? *`(run_date, provider_txn_id, kind, cause)`.*
3. What does `ON CONFLICT (run_date) DO UPDATE` do? *Updates the existing row for that day instead of failing or adding a second one.*
4. How does `HAVING` differ from `WHERE`? *`HAVING` filters groups after `GROUP BY`; `WHERE` filters rows before.*
5. Why a half-open range for a day? *So that no instant belongs to two days.*

## Further reading

- PostgreSQL 18, SQL language: https://www.postgresql.org/docs/18/sql.html
- Data definition: https://www.postgresql.org/docs/18/ddl.html
- Queries: https://www.postgresql.org/docs/18/queries.html
- `INSERT ... ON CONFLICT`: https://www.postgresql.org/docs/18/sql-insert.html
- PL/pgSQL: https://www.postgresql.org/docs/18/plpgsql.html
- `psql` meta-commands (`\echo`, `\dt`): https://www.postgresql.org/docs/18/app-psql.html
