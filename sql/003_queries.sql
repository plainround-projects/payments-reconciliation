-- 003_queries.sql: the queries the lesson runs. Run with `make queries`.

\echo '== 1. Trial balance: the raw sum and the balance on its normal side, per account'
SELECT a.code AS account,
       a.normal_side,
       COALESCE(sum(p.amount_minor), 0) AS raw_sum_minor,
       COALESCE(sum(p.amount_minor) * CASE a.normal_side WHEN 'DEBIT' THEN 1 ELSE -1 END, 0) AS balance_minor
  FROM accounts a LEFT JOIN postings p ON p.account = a.code
 GROUP BY a.code, a.normal_side
 ORDER BY a.code;

\echo '== 2. The ledger invariant: the grand total of every posting (must be 0), and entries that are not balanced pairs (none)'
SELECT COALESCE(sum(amount_minor), 0) AS ledger_sum_minor FROM postings;
SELECT entry_id, count(*) AS rows, sum(amount_minor) AS total
  FROM postings GROUP BY entry_id HAVING count(*) <> 2 OR sum(amount_minor) <> 0;

\echo '== 3. Captured payments per UTC day: count, gross, fees (the ledger side of a reconciliation)'
SELECT captured_at::date AS day, count(*) FILTER (WHERE state = 'CAPTURED') AS captured,
       sum(amount_minor) FILTER (WHERE state = 'CAPTURED') AS gross_minor,
       sum(fee_minor) FILTER (WHERE state = 'CAPTURED') AS fees_minor,
       count(*) FILTER (WHERE state = 'DUPLICATE_REVERSED') AS reversed_duplicates
  FROM payments GROUP BY 1 ORDER BY 1;

\echo '== 4. Provider transaction ids with more than one active payment (the duplicate break; none after resolution)'
SELECT provider_txn_id, count(*) AS active_payments, array_agg(source ORDER BY captured_at) AS sources
  FROM payments WHERE state = 'CAPTURED'
 GROUP BY provider_txn_id HAVING count(*) > 1;

\echo '== 5. Payments without their capture rows: an active payment must have six postings (four when the fee is 0); no rows'
SELECT pay.id, pay.provider_txn_id, count(p.id) FILTER (WHERE e.operation = 'CAPTURE') AS capture_postings
  FROM payments pay
  LEFT JOIN journal_entries e ON e.payment_id = pay.id
  LEFT JOIN postings p ON p.entry_id = e.id
 GROUP BY pay.id, pay.provider_txn_id, pay.amount_minor, pay.fee_minor
HAVING count(p.id) FILTER (WHERE e.operation = 'CAPTURE')
       <> 2 + CASE WHEN pay.amount_minor - pay.fee_minor > 0 THEN 2 ELSE 0 END
           + CASE WHEN pay.fee_minor > 0 THEN 2 ELSE 0 END;

\echo '== 6. Breaks by cause and status (every run)'
SELECT cause, status, count(*) AS breaks FROM reconciliation_breaks GROUP BY cause, status ORDER BY cause, status;

\echo '== 7. The worklist: breaks that need a person, oldest first'
SELECT run_date, provider_txn_id, cause, ledger_amount_minor, settlement_amount_minor, note
  FROM reconciliation_breaks WHERE status = 'NEEDS_REVIEW' ORDER BY run_date, provider_txn_id;

\echo '== 8. Timing differences still waiting for a report, and how long they have waited'
SELECT provider_txn_id, run_date, (current_date - run_date) AS days_waiting
  FROM reconciliation_breaks WHERE status = 'CARRIED' ORDER BY run_date;

\echo '== 9. Each run: the first-pass numbers'
SELECT run_date, settlement_rows, settled_total_minor, refunded_total_minor, clean_transactions, clean_total_minor, break_count
  FROM reconciliation_runs ORDER BY run_date;

\echo '== 10. What the resolutions posted, by operation'
SELECT e.operation, e.leg, count(DISTINCT e.id) AS entries, sum(p.amount_minor) FILTER (WHERE p.amount_minor > 0) AS debits_minor
  FROM journal_entries e JOIN postings p ON p.entry_id = e.id
 WHERE e.operation <> 'CAPTURE'
 GROUP BY e.operation, e.leg ORDER BY e.operation, e.leg;
