-- Steps 4 and 5 of the lesson: resolve the breaks. Fee corrections, reversals of duplicates, refunds posted from the
-- report, a payment inserted from the report; each is its own balanced entries and posts once.
--
-- Money is a count of minor units (BIGINT) with a currency. In postings a positive amount is a debit and a negative
-- amount is a credit. The constraints make an unbalanced posting impossible, an edit to the ledger impossible, and
-- a second posting of the same operation impossible.

BEGIN;

ALTER TABLE payments DROP CONSTRAINT payments_state_valid;
ALTER TABLE payments DROP CONSTRAINT payments_source_valid;
ALTER TABLE payments
    ADD CONSTRAINT payments_state_valid CHECK (state IN ('CAPTURED', 'DUPLICATE_REVERSED')),
    ADD CONSTRAINT payments_source_valid CHECK (source IN ('SERVICE', 'SERVICE_RETRY', 'RECONCILIATION'));

ALTER TABLE journal_entries DROP CONSTRAINT journal_entries_operation_valid;
ALTER TABLE journal_entries DROP CONSTRAINT journal_entries_leg_valid;
ALTER TABLE journal_entries
    ADD CONSTRAINT journal_entries_operation_valid CHECK (operation IN ('CAPTURE', 'REFUND', 'FEE_CORRECTION', 'REVERSAL')),
    ADD CONSTRAINT journal_entries_leg_valid CHECK (leg IN (
        'CAPTURE_GROSS', 'CAPTURE_NET', 'CAPTURE_FEE', 'REFUND_GROSS', 'REFUND_NET', 'FEE_CORRECTION',
        'REVERSAL_GROSS', 'REVERSAL_NET', 'REVERSAL_FEE' 
    ));

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

COMMIT;
