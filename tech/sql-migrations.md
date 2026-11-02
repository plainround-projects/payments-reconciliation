# SQL walkthrough: the migrations, line by line

## What it is

This note reads the three files in `sql/migrations/`. A **migration** is a numbered SQL file that moves a database from one schema version to the next. Applying them in order gives the same schema as `sql/001_schema.sql`; an integration test compares the two.

## Why this repository uses it

A real database already holds data, so it cannot be dropped and rebuilt; it has to be changed step by step. The migrations show that path and follow the steps of the lesson: both sides, then the matcher's tables, then the resolutions.

## Where it appears

| File | What it is |
| --- | --- |
| `sql/migrations/001_payments_and_ledger.sql` | Step 1: payments and the ledger. |
| `sql/migrations/002_reconciliation.sql` | Steps 2 and 3: runs and breaks. |
| `sql/migrations/003_resolutions.sql` | Steps 4 and 5: widen the checks, add refunds. |

## How it works underneath

`ALTER TABLE` changes a table that exists. When a constraint is added, PostgreSQL checks the existing rows; if any breaks it the statement fails and, inside a transaction, nothing changes. A rule that must change is dropped and added again. Each migration is wrapped in `BEGIN;` ... `COMMIT;` so it applies completely or not at all.

## Walkthrough of this repository's use



### `sql/migrations/001_payments_and_ledger.sql` (127 lines)

```sql
-- Step 1 of the lesson: produce both sides. Payments that carry the provider's transaction id, and the ledger
-- they post to (the idempotent-payments project's ledger, with the same append-only and balanced-pair rules).
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;
```

Migration 1 (step 1 of the lesson): produce both sides, the payments that carry the provider's transaction id and the ledger they post to. A **migration** is a numbered SQL file that moves the database one step; applying them in order builds the same schema as `001_schema.sql` (a test proves it). The header repeats the money convention. `BEGIN;` opens a transaction.

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
    CONSTRAINT payments_state_valid CHECK (state IN ('CAPTURED')),
    CONSTRAINT payments_source_valid CHECK (source IN ('SERVICE'))
);
COMMENT ON TABLE payments IS
    'Captured payments, each carrying the provider''s own transaction id. provider_txn_id is deliberately NOT unique: '
    'this is the ledger under reconciliation, and a duplicate row for one provider transaction is a break to find, not '
    'a write to refuse. The payments service upstream prevents duplicates with its idempotency keys.';
CREATE INDEX payments_provider_txn_id_idx ON payments (provider_txn_id);
CREATE INDEX payments_captured_at_idx ON payments (captured_at);
```

The **payments** table: one row per captured payment, carrying the provider's own transaction id. `UUID` is a 128-bit identifier, `TEXT` is text, `CHAR(3)` is exactly three characters, `BIGINT` a 64-bit whole number, `TIMESTAMPTZ` a point in time with a time zone. `refunded_minor BIGINT NOT NULL DEFAULT 0` starts at zero. `idempotency_key TEXT UNIQUE` may be `NULL` but, when present, no two rows share it. The named constraints, each a rule every row must satisfy: the provider id must look like `ptx_` followed by 1 to 32 letters or digits (`~` means "matches this regular expression"); the currency is three capital letters; the amount is positive; the fee lies between 0 and the amount (`BETWEEN a AND b` includes both ends); the refunded total lies between 0 and the amount; at this step the state may only be `CAPTURED` and the source only `SERVICE` (migration 3 widens both). `COMMENT ON TABLE ... IS '...'` stores a description inside the database, and the comment says why `provider_txn_id` is **deliberately not unique**: this is the ledger under reconciliation, and a duplicate row is a break to find, not a write to refuse. Two indexes follow (an **index** is a sorted side structure that makes lookups fast): one on the provider id, which the join uses, and one on the capture time, which the daily query uses.

```sql
CREATE TABLE journal_entries (
    id         UUID PRIMARY KEY,
    payment_id UUID        NOT NULL REFERENCES payments (id),
    operation  TEXT        NOT NULL,
    reference  TEXT        NOT NULL,
    leg        TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT journal_entries_operation_valid CHECK (operation IN ('CAPTURE')),
    CONSTRAINT journal_entries_leg_valid CHECK (leg IN ('CAPTURE_GROSS', 'CAPTURE_NET', 'CAPTURE_FEE')),
    CONSTRAINT journal_entries_once_per_leg UNIQUE (payment_id, operation, reference, leg),
    CONSTRAINT journal_entries_id_payment_key UNIQUE (id, payment_id)
);
COMMENT ON TABLE journal_entries IS
    'One entry = one debit row and one equal credit row. UNIQUE (payment, operation, reference, leg) means each '
    'operation posts once: a capture, a fee correction to a given fee, a reversal of a given duplicate, a refund.';
```

A **journal entry** groups the two rows of one debit/credit pair. `payment_id UUID NOT NULL REFERENCES payments (id)` is a **foreign key**: the value must exist in `payments`. `operation` says what happened, `reference` says which instance (`capture`, a refund reference, the fee or duplicate being corrected), and `leg` names the part. At this step only the capture operation and its three legs are allowed (migration 3 widens them). `UNIQUE (payment_id, operation, reference, leg)` means that combination can exist only once, which is how each operation posts once. `UNIQUE (id, payment_id)` exists so that postings can reference the pair. The comment explains the rule in words.

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
COMMIT;
```

`COMMIT;` applies the migration.

### `sql/migrations/002_reconciliation.sql` (65 lines)

```sql
-- Steps 2 and 3 of the lesson: match on the stable id and classify. A run per day and the breaks it finds.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;
```

Migration 2 (steps 2 and 3 of the lesson): match on the stable id and classify. It adds the two tables for a run per day and the breaks it finds. The header repeats the money convention.

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

The **refunds** table: an id, the payment (a foreign key), a positive amount, the currency, a `reference` text and the time. `UNIQUE (payment_id, reference)` means a payment can have only one refund with a given reference, so the same refund cannot be recorded twice. An index on `created_at` supports the daily query of refunds recorded on a day.

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
```

One row per business day reconciled: `run_date DATE PRIMARY KEY` (a calendar date, unique), the currency, the counts and totals of the **first pass** (ledger payments, settlement rows, settled and refunded totals, clean transactions and their total, break count), when it ran, `reruns` (how many later runs of the same day happened, starting at 0) and when the last one ran. The check `reconciliation_runs_counts_valid` forbids negative counts (`AND` joins conditions). The comment explains why the first-pass numbers are kept: the report must show them after the breaks are fixed.

```sql
COMMIT;
```

`COMMIT;` applies the migration.

### `sql/migrations/003_resolutions.sql` (39 lines)

```sql
-- Steps 4 and 5 of the lesson: resolve the breaks. Fee corrections, reversals of duplicates, refunds posted from the
-- report, a payment inserted from the report; each is its own balanced entries and posts once.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;
```

Migration 3 (steps 4 and 5 of the lesson): resolve the breaks. Fee corrections, reversals of duplicates, refunds posted from the report and a payment inserted from the report are each their own balanced entries and post once. The header repeats the money convention.

```sql
ALTER TABLE payments DROP CONSTRAINT payments_state_valid;
```

`ALTER TABLE` changes an existing table. A check constraint cannot be edited, so it is dropped and a new one is added. The first two statements drop the old state and source checks.

```sql
ALTER TABLE payments DROP CONSTRAINT payments_source_valid;
ALTER TABLE payments
    ADD CONSTRAINT payments_state_valid CHECK (state IN ('CAPTURED', 'DUPLICATE_REVERSED')),
    ADD CONSTRAINT payments_source_valid CHECK (source IN ('SERVICE', 'SERVICE_RETRY', 'RECONCILIATION'));
```

One `ALTER TABLE` with two `ADD CONSTRAINT` clauses (separated by a comma) adds the wider checks: a payment may now also be `DUPLICATE_REVERSED`, and its source may now also be `SERVICE_RETRY` or `RECONCILIATION`.

```sql
ALTER TABLE journal_entries DROP CONSTRAINT journal_entries_operation_valid;
```

The same for the journal: the old operation and leg checks are dropped.

```sql
ALTER TABLE journal_entries DROP CONSTRAINT journal_entries_leg_valid;
ALTER TABLE journal_entries
    ADD CONSTRAINT journal_entries_operation_valid CHECK (operation IN ('CAPTURE', 'REFUND', 'FEE_CORRECTION', 'REVERSAL')),
    ADD CONSTRAINT journal_entries_leg_valid CHECK (leg IN (
        'CAPTURE_GROSS', 'CAPTURE_NET', 'CAPTURE_FEE', 'REFUND_GROSS', 'REFUND_NET', 'FEE_CORRECTION',
        'REVERSAL_GROSS', 'REVERSAL_NET', 'REVERSAL_FEE' 
    ));
```

The new checks allow the operations `CAPTURE`, `REFUND`, `FEE_CORRECTION` and `REVERSAL`, and the nine legs.

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

The **append-only** rule. The function always raises an error saying `<table> is append-only; post a correcting entry instead` (`TG_TABLE_NAME` is the name of the table that fired it, `ERRCODE = 'object_not_in_prerequisite_state'` is the error class). The two `CREATE TRIGGER ... BEFORE UPDATE OR DELETE ON ... FOR EACH ROW EXECUTE FUNCTION forbid_mutation();` statements attach it to `postings` and `journal_entries`, so a posted row can never be edited or removed: a mistake is corrected by a new, balanced entry.

```sql
COMMIT;
```

`COMMIT;` applies the migration. Constraints on existing rows are checked when they are added, so a migration cannot leave rows that break a rule.

## Try it yourself

1. Apply them to a scratch database and list the tables:

   ```bash
   make up
   make migrate
   ```

   Expected: a list that includes `accounts`, `payments`, `journal_entries`, `postings`, `reconciliation_runs`, `reconciliation_breaks` and `refunds`.

2. Compare the schemas: run the integration test `test_the_migrations_build_the_same_schema_as_001_schema` with `.venv/bin/python -m pytest tests/integration -k migrations`.

## Mistakes people make

- **Editing an old migration after it has been applied.** Add a new one instead.
- **Altering a constraint in place.** Drop it and add the new one in the same migration.
- **Letting a migration leave the schema different from the direct file.** The comparison test catches it.

## Check yourself

1. Why drop and re-add a check constraint? *A check cannot be edited.*
2. Which migration adds the refunds table? *The third.*
3. What makes a migration all-or-nothing? *`BEGIN;` and `COMMIT;` around it.*
4. What does the comparison test prove? *Migrations 1 to 3 give the same schema as `001_schema.sql`.*
5. Why is the state check widened in migration 3? *Resolutions create reversed duplicates and payments inserted from the report.*

## Further reading

- PostgreSQL 18 `ALTER TABLE`: https://www.postgresql.org/docs/18/sql-altertable.html
- PostgreSQL 18 transactions: https://www.postgresql.org/docs/18/tutorial-transactions.html
