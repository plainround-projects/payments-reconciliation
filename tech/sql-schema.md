# SQL walkthrough: the schema, line by line

## What it is

This note reads the two SQL files that build and empty the database: `sql/001_schema.sql` and `sql/reset.sql`. SQL is the language for talking to a relational database; these files use the **DDL** part (data definition language: `CREATE TABLE`, `CREATE INDEX`, `CREATE FUNCTION`, `CREATE TRIGGER`), which describes structure and rules. It is the line-by-line companion of `sql.md` and `postgresql.md`.

## Why this repository uses it

The lesson's ledger must be trustworthy even if the program has a bug. The schema puts the rules in the database, where they cannot be bypassed: an entry that is not a balanced pair cannot commit, a posted row cannot be edited or deleted, and each operation can be posted only once. The same schema adds the tables that hold the reconciliation: one row per run and one row per difference found.

## Where it appears

| File | What it is |
| --- | --- |
| `sql/001_schema.sql` | The whole schema. |
| `sql/reset.sql` | Drops everything so `make db` starts empty. |

## How it works underneath

PostgreSQL checks every constraint when a row is written; a violation raises an error and the whole transaction is rolled back. A *deferred* constraint trigger waits until `COMMIT` to run, so several statements can be applied before the combined result is checked. A *trigger* is a function that PostgreSQL calls automatically on `INSERT`, `UPDATE` or `DELETE`. A *foreign key* makes one table's value refer to a row of another. An *index* is a sorted copy of some columns that makes lookups fast. A *unique* constraint is enforced with a unique index.

## Walkthrough of this repository's use



### `sql/001_schema.sql` (197 lines)

```sql
-- 001_schema.sql: the complete schema (DDL). Run by `make db`; the stepwise path to the same schema is in migrations/.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;
```

The header comment explains the idea: money is a count of minor units in a `BIGINT` (a 64-bit whole number) with a currency, never a float; in `postings` a positive amount is a debit and a negative one a credit; and the constraints below make three things impossible whatever the application does: an unbalanced posting, an edit to the ledger, and a second posting of the same operation. The comment also says this file is the complete schema and that the same schema can be reached step by step through the `migrations/` files.

`BEGIN;` starts a **transaction**: the statements up to `COMMIT;` at the end are applied together or not at all. If any statement fails, nothing is left half-built.

```sql
CREATE TABLE accounts (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    normal_side TEXT NOT NULL,
    CONSTRAINT accounts_normal_side_valid CHECK (normal_side IN ('DEBIT', 'CREDIT'))
);
COMMENT ON TABLE accounts IS 'The chart of accounts. normal_side is the side on which the balance reads positive.';
```

The **chart of accounts**. `CREATE TABLE name (...)` defines a table; each line inside is a column with a **type** and rules. `code TEXT PRIMARY KEY`: text, unique and never empty, identifying the row. `name TEXT NOT NULL`: text that must be given. `normal_side TEXT NOT NULL`: also required. `CONSTRAINT accounts_normal_side_valid CHECK (normal_side IN ('DEBIT', 'CREDIT'))` gives the rule a name and says the value must be one of the two words. `COMMENT ON TABLE ... IS '...'` stores a description inside the database.

```sql
CREATE TABLE payments (
    id              UUID PRIMARY KEY,
    provider_txn_id TEXT        NOT NULL,
    currency        CHAR(3)     NOT NULL,
    amount_minor    BIGINT      NOT NULL,
    fee_minor       BIGINT      NOT NULL,
    state           TEXT        NOT NULL,
    source          TEXT        NOT NULL,
    refunded_minor  BIGINT      NOT NULL DEFAULT 0,
    idempotency_key TEXT UNIQUE,
    captured_at     TIMESTAMPTZ NOT NULL,
    CONSTRAINT payments_txn_id_format CHECK (provider_txn_id ~ '^ptx_[A-Za-z0-9]{1,32}$'),
    CONSTRAINT payments_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT payments_amount_positive CHECK (amount_minor > 0),
    CONSTRAINT payments_fee_bounds CHECK (fee_minor BETWEEN 0 AND amount_minor),
    CONSTRAINT payments_refund_bounds CHECK (refunded_minor BETWEEN 0 AND amount_minor),
    CONSTRAINT payments_state_valid CHECK (state IN ('CAPTURED', 'DUPLICATE_REVERSED')),
    CONSTRAINT payments_source_valid CHECK (source IN ('SERVICE', 'SERVICE_RETRY', 'RECONCILIATION'))
);
COMMENT ON TABLE payments IS
    'Captured payments, each carrying the provider''s own transaction id. provider_txn_id is deliberately NOT unique: '
    'this is the ledger under reconciliation, and a duplicate row for one provider transaction is a break to find, not '
    'a write to refuse. The payments service upstream prevents duplicates with its idempotency keys.';
CREATE INDEX payments_provider_txn_id_idx ON payments (provider_txn_id);
CREATE INDEX payments_captured_at_idx ON payments (captured_at);
```

The **payments** table: one row per captured payment, carrying the provider's own transaction id. `UUID` is a 128-bit identifier, `TEXT` is text, `CHAR(3)` is exactly three characters, `BIGINT` a 64-bit whole number, `TIMESTAMPTZ` a point in time with a time zone. `refunded_minor BIGINT NOT NULL DEFAULT 0` starts at zero. `idempotency_key TEXT UNIQUE` may be `NULL` but, when present, no two rows share it. The named constraints, each a rule every row must satisfy: the provider id must look like `ptx_` followed by 1 to 32 letters or digits (`~` means "matches this regular expression"); the currency is three capital letters; the amount is positive; the fee lies between 0 and the amount (`BETWEEN a AND b` includes both ends); the refunded total lies between 0 and the amount; the state is `CAPTURED` or `DUPLICATE_REVERSED`; the source is `SERVICE`, `SERVICE_RETRY` or `RECONCILIATION`. `COMMENT ON TABLE ... IS '...'` stores a description inside the database, and the comment says why `provider_txn_id` is **deliberately not unique**: this is the ledger under reconciliation, and a duplicate row is a break to find, not a write to refuse. Two indexes follow (an **index** is a sorted side structure that makes lookups fast): one on the provider id, which the join uses, and one on the capture time, which the daily query uses.

```sql
CREATE TABLE journal_entries (
    id         UUID PRIMARY KEY,
    payment_id UUID        NOT NULL REFERENCES payments (id),
    operation  TEXT        NOT NULL,
    reference  TEXT        NOT NULL,
    leg        TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT journal_entries_operation_valid CHECK (operation IN ('CAPTURE', 'REFUND', 'FEE_CORRECTION', 'REVERSAL')),
    CONSTRAINT journal_entries_leg_valid CHECK (leg IN ('CAPTURE_GROSS', 'CAPTURE_NET', 'CAPTURE_FEE', 'REFUND_GROSS', 'REFUND_NET', 'FEE_CORRECTION',
        'REVERSAL_GROSS', 'REVERSAL_NET', 'REVERSAL_FEE')),
    CONSTRAINT journal_entries_once_per_leg UNIQUE (payment_id, operation, reference, leg),
    CONSTRAINT journal_entries_id_payment_key UNIQUE (id, payment_id)
);
COMMENT ON TABLE journal_entries IS
    'One entry = one debit row and one equal credit row. UNIQUE (payment, operation, reference, leg) means each '
    'operation posts once: a capture, a fee correction to a given fee, a reversal of a given duplicate, a refund.';
```

A **journal entry** groups the two rows of one debit/credit pair. `payment_id UUID NOT NULL REFERENCES payments (id)` is a **foreign key**: the value must exist in `payments`. `operation` says what happened, `reference` says which instance (`capture`, a refund reference, the fee or duplicate being corrected), and `leg` is one of nine names (the three capture legs, the two refund legs, the fee correction and the three reversal legs). The `operation` is one of `CAPTURE`, `REFUND`, `FEE_CORRECTION`, `REVERSAL`. `UNIQUE (payment_id, operation, reference, leg)` means that combination can exist only once, which is how each operation posts once. `UNIQUE (id, payment_id)` exists so that postings can reference the pair. The comment explains the rule in words.

```sql
CREATE TABLE postings (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id     UUID        NOT NULL,
    payment_id   UUID        NOT NULL,
    account      TEXT        NOT NULL REFERENCES accounts (code),
    amount_minor BIGINT      NOT NULL,
    currency     CHAR(3)     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT postings_amount_nonzero CHECK (amount_minor <> 0),
    CONSTRAINT postings_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT postings_entry_fk FOREIGN KEY (entry_id, payment_id) REFERENCES journal_entries (id, payment_id)
);
COMMENT ON TABLE postings IS 'One row per debit (positive) or credit (negative). Append-only; the sum of the table is zero.';
CREATE INDEX postings_entry_id_idx ON postings (entry_id);
CREATE INDEX postings_payment_id_idx ON postings (payment_id);
CREATE INDEX postings_account_idx ON postings (account);

-- Every entry must end up as exactly two rows on two accounts, in one currency, summing to zero. The check is
-- deferred to COMMIT so the two rows can be inserted one after the other.
```

A **posting** is one row of the ledger. `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` is a number the database assigns (1, 2, 3 ...) and refuses to accept from the program. `account TEXT NOT NULL REFERENCES accounts (code)` must be a known account. `amount_minor BIGINT NOT NULL` is signed. `postings_amount_nonzero` forbids zero. `postings_entry_fk` is a foreign key on **two columns** `(entry_id, payment_id)` that must match an existing `(id, payment_id)` of `journal_entries`: a posting's payment must be the same as its entry's. Three indexes follow, on `entry_id`, `payment_id` and `account`.

```sql
CREATE FUNCTION assert_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    row_count      INTEGER;
    total          BIGINT;
    account_count  INTEGER;
    currency_count INTEGER;
BEGIN
    SELECT count(*), sum(amount_minor), count(DISTINCT account), count(DISTINCT currency)
      INTO row_count, total, account_count, currency_count
      FROM postings WHERE entry_id = NEW.entry_id;
    IF row_count <> 2 OR total <> 0 OR account_count <> 2 OR currency_count <> 1 THEN
        RAISE EXCEPTION 'journal entry % is not a balanced pair (rows %, sum %, accounts %, currencies %)',
            NEW.entry_id, row_count, total, account_count, currency_count
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
```

A **trigger function** written in PL/pgSQL (PostgreSQL's built-in programming language). `$$ ... $$` quotes the body. `DECLARE` introduces four variables. In the body, `SELECT count(*), sum(amount_minor), count(DISTINCT account), count(DISTINCT currency) INTO row_count, total, account_count, currency_count FROM postings WHERE entry_id = NEW.entry_id;` measures the entry: how many rows, what they sum to, how many different accounts and currencies. `NEW` is the row that fired the trigger. If the entry is not exactly two rows on two accounts in one currency summing to zero, `RAISE EXCEPTION '... % ...', args USING ERRCODE = 'check_violation'` stops the transaction with a message (each `%` is replaced by the next argument). `RETURN NULL` is the normal ending for an `AFTER` trigger.

```sql
CREATE CONSTRAINT TRIGGER postings_entry_balanced
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();

-- An entry without postings would be invisible to the check above, so require its two rows at COMMIT too.
```

The trigger itself. `CONSTRAINT TRIGGER ... AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ...` fires after each inserted posting but, because it is *deferred*, only **at `COMMIT`**. That is needed because the two rows of an entry are inserted one after the other; the check is meaningful only when both are in.

```sql
CREATE FUNCTION assert_entry_has_postings() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM postings WHERE entry_id = NEW.id) <> 2 THEN
        RAISE EXCEPTION 'journal entry % has no balanced pair of postings', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
```

A second function guards the case the first cannot see: an entry with **no** postings never fires the first trigger. This one counts the postings of a new journal entry at `COMMIT` and raises an error unless there are exactly two. It is attached with another deferred constraint trigger on `journal_entries`.

```sql
CREATE CONSTRAINT TRIGGER journal_entries_have_postings
    AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_has_postings();

-- The ledger is append-only: a mistake is corrected by a new, balanced entry, never by an edit or a delete.
```

The deferred trigger on `journal_entries`.

```sql
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; post a correcting entry instead', TG_TABLE_NAME
        USING ERRCODE = 'object_not_in_prerequisite_state';
END
$$;
CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER journal_entries_append_only BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

The **append-only** rule. The function always raises an error saying `<table> is append-only; post a correcting entry instead` (`TG_TABLE_NAME` is the name of the table that fired it, `ERRCODE = 'object_not_in_prerequisite_state'` is the error class). The two `CREATE TRIGGER ... BEFORE UPDATE OR DELETE ON ... FOR EACH ROW EXECUTE FUNCTION forbid_mutation();` statements attach it to `postings` and `journal_entries`, so a posted row can never be edited or removed: a mistake is corrected by a new, balanced entry.

```sql
CREATE TABLE refunds (
    id           UUID PRIMARY KEY,
    payment_id   UUID        NOT NULL REFERENCES payments (id),
    amount_minor BIGINT      NOT NULL,
    currency     CHAR(3)     NOT NULL,
    reference    TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT refunds_amount_positive CHECK (amount_minor > 0),
    CONSTRAINT refunds_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT refunds_once_per_reference UNIQUE (payment_id, reference)
);
COMMENT ON TABLE refunds IS 'Refunds recorded in the ledger; each has its own balanced entries in journal_entries.';
CREATE INDEX refunds_created_at_idx ON refunds (created_at);
```

The **refunds** table: an id, the payment (a foreign key), a positive amount, the currency, a `reference` text and the time. `UNIQUE (payment_id, reference)` means a payment can have only one refund with a given reference, so the same refund cannot be recorded twice. An index on `created_at` supports the daily query of refunds recorded on a day.

```sql
CREATE TABLE reconciliation_runs (
    run_date            DATE PRIMARY KEY,
    currency            CHAR(3)     NOT NULL,
    ledger_payments     INTEGER     NOT NULL,
    settlement_rows     INTEGER     NOT NULL,
    settled_total_minor BIGINT      NOT NULL,
    refunded_total_minor BIGINT     NOT NULL,
    clean_transactions  INTEGER     NOT NULL,
    clean_total_minor   BIGINT      NOT NULL,
    break_count         INTEGER     NOT NULL,
    ran_at              TIMESTAMPTZ NOT NULL,
    reruns              INTEGER     NOT NULL DEFAULT 0,
    last_ran_at         TIMESTAMPTZ NOT NULL,
    CONSTRAINT reconciliation_runs_counts_valid CHECK (
        ledger_payments >= 0 AND settlement_rows >= 0 AND clean_transactions >= 0 AND break_count >= 0
        AND reruns >= 0
    )
);
COMMENT ON TABLE reconciliation_runs IS
    'The first-pass numbers of each daily run, kept so the report can show them after the breaks are fixed; a later run of the same day only counts itself in reruns and last_ran_at.';
```

One row per business day reconciled: `run_date DATE PRIMARY KEY` (a calendar date, unique), the currency, the counts and totals of the **first pass** (ledger payments, settlement rows, settled and refunded totals, clean transactions and their total, break count), when it ran, `reruns` (how many later runs of the same day happened, starting at 0) and when the last one ran. The check `reconciliation_runs_counts_valid` forbids negative counts (`AND` joins conditions). The comment explains why the first-pass numbers are kept: the report must show them after the breaks are fixed.

```sql
CREATE TABLE reconciliation_breaks (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_date                DATE        NOT NULL REFERENCES reconciliation_runs (run_date),
    provider_txn_id         TEXT        NOT NULL,
    kind                    TEXT        NOT NULL,
    cause                   TEXT        NOT NULL,
    status                  TEXT        NOT NULL,
    currency                CHAR(3)     NOT NULL,
    ledger_amount_minor     BIGINT,
    settlement_amount_minor BIGINT,
    ledger_fee_minor        BIGINT,
    settlement_fee_minor    BIGINT,
    detail                  TEXT        NOT NULL,
    note                    TEXT        NOT NULL,
    cleared_on              DATE,
    created_at              TIMESTAMPTZ NOT NULL,
    updated_at              TIMESTAMPTZ NOT NULL,
    CONSTRAINT reconciliation_breaks_natural_key UNIQUE (run_date, provider_txn_id, kind, cause),
    CONSTRAINT reconciliation_breaks_kind_valid CHECK (kind IN ('settled', 'refunded')),
    CONSTRAINT reconciliation_breaks_cause_valid CHECK (cause IN (
        'timing', 'fee_discrepancy', 'missing_capture', 'duplicate_on_our_side', 'provider_only_refund',
        'amount_mismatch', 'ledger_only_unexplained', 'timing_overdue', 'provider_duplicate',
        'refund_only_in_ledger', 'refund_amount_mismatch'
    )),
    CONSTRAINT reconciliation_breaks_status_valid CHECK (status IN (
        'OPEN', 'CARRIED', 'AUTO_RESOLVED', 'RESOLVED', 'NEEDS_REVIEW', 'CLEARED', 'ESCALATED'
    )),
    CONSTRAINT reconciliation_breaks_cleared_on_matches_status CHECK ((cleared_on IS NOT NULL) <= (status = 'CLEARED'))
);
COMMENT ON TABLE reconciliation_breaks IS
    'One row per difference found: keyed by (day, provider transaction id, kind, cause), so running a day again '
    'updates the same rows instead of piling up new ones.';
CREATE INDEX reconciliation_breaks_status_idx ON reconciliation_breaks (status);
CREATE INDEX reconciliation_breaks_txn_idx ON reconciliation_breaks (provider_txn_id);

COMMIT;
```

One row per difference found. `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` is a number the database assigns (1, 2, 3 ...). `run_date` is a foreign key to the run. `kind` is `settled` or `refunded`; `cause` is one of the eleven causes of `reconciliation.py`; `status` is one of the seven statuses. The four amount columns have no `NOT NULL` because one side of a break may have no amount. `detail` is the explanation written by the matcher and `note` the latest message. `cleared_on` is set when a carried break settles. `reconciliation_breaks_natural_key UNIQUE (run_date, provider_txn_id, kind, cause)` is why running a day again updates the same rows instead of piling up new ones. The last check, `(cleared_on IS NOT NULL) <= (status = 'CLEARED')`, uses the fact that for true/false values `<=` means "implies": a break with a clearing date must have the status `CLEARED`. Two indexes support finding breaks by status and by transaction. The file ends with `COMMIT;`, which applies everything.

### `sql/reset.sql` (4 lines)

```sql
-- reset.sql: drop everything so `make db` starts from an empty database. The ledger is append-only, so a reset is
-- a schema drop, not a DELETE. Local development only.
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
```

Two statements that empty the database. `DROP SCHEMA public CASCADE;` deletes the schema called `public` (the default namespace that holds all the tables) together with everything inside it: `CASCADE` means "and everything that depends on it". `CREATE SCHEMA public;` makes a new empty one. A schema drop is used instead of `DELETE` because the ledger tables refuse updates and deletes by design. The comment says it is for local development only.

## Try it yourself

1. Build the schema and the seed:

   ```bash
   make up
   ```

   Expected: PostgreSQL starts and the schema and seed are loaded.

2. List the tables:

   ```bash
   docker compose exec -T postgres psql -U reconciliation -d reconciliation -c '\dt'
   ```

   Expected: `accounts`, `journal_entries`, `payments`, `postings`, `reconciliation_breaks`, `reconciliation_runs`, `refunds`.

3. Try to break a rule (the transaction is rolled back, nothing is changed):

   ```bash
   docker compose exec -T postgres psql -U reconciliation -d reconciliation -c "UPDATE postings SET amount_minor = 1 WHERE id = 1"
   ```

   Expected: `ERROR:  postings is append-only; post a correcting entry instead`.

## Mistakes people make

- **Making `provider_txn_id` unique.** Here a duplicate is a break to find, not a write to refuse.
- **Editing a posted row to fix a mistake.** The triggers forbid it; post a correcting entry.
- **Checking balance after every row.** The two rows of an entry arrive one after the other; the check is deferred to commit.
- **Storing money in a float column.** Amounts are `BIGINT` minor units.

## Check yourself

1. Why is the balance trigger deferred? *The two rows of an entry are inserted one after the other; the pair is checked at commit.*
2. What makes a posted row permanent? *The `forbid_mutation` triggers on update and delete.*
3. What does `UNIQUE (payment_id, operation, reference, leg)` prevent? *Posting the same leg of the same operation twice.*
4. Why is `reconciliation_breaks` unique on four columns? *A re-run updates the same rows instead of adding new ones.*
5. What does `(cleared_on IS NOT NULL) <= (status = 'CLEARED')` mean? *A break with a clearing date must have the status CLEARED.*

## Further reading

- PostgreSQL 18 `CREATE TABLE`: https://www.postgresql.org/docs/18/sql-createtable.html
- PostgreSQL 18 `CREATE TRIGGER`: https://www.postgresql.org/docs/18/sql-createtrigger.html
- PostgreSQL 18 constraints: https://www.postgresql.org/docs/18/ddl-constraints.html
- PostgreSQL 18 PL/pgSQL: https://www.postgresql.org/docs/18/plpgsql.html
