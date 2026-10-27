-- Steps 2 and 3 of the lesson: match on the stable id and classify. A run per day and the breaks it finds.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;

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
