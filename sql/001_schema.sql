-- 001_schema.sql: the complete schema (DDL). Run by `make db`; the stepwise path to the same schema is in migrations/.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;

CREATE TABLE accounts (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    normal_side TEXT NOT NULL,
    CONSTRAINT accounts_normal_side_valid CHECK (normal_side IN ('DEBIT', 'CREDIT'))
);
COMMENT ON TABLE accounts IS 'The chart of accounts. normal_side is the side on which the balance reads positive.';

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
CREATE CONSTRAINT TRIGGER postings_entry_balanced
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();

-- An entry without postings would be invisible to the check above, so require its two rows at COMMIT too.
CREATE FUNCTION assert_entry_has_postings() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM postings WHERE entry_id = NEW.id) <> 2 THEN
        RAISE EXCEPTION 'journal entry % has no balanced pair of postings', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER journal_entries_have_postings
    AFTER INSERT ON journal_entries DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_has_postings();

-- The ledger is append-only: a mistake is corrected by a new, balanced entry, never by an edit or a delete.
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
