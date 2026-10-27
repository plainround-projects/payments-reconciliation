# 1. Join on the provider's transaction id, and classify every difference by its cause

Status: accepted, 2026-10-27

## Context

The ledger and the provider's file describe the same money from two sides. Matching by amount and time pairs unrelated transactions that happen to agree, and hides a real break behind a clean result. Lumping every difference into "mismatch" hides that a timing difference needs nothing today while a missing capture needs money added to the books.

## Decision

The only join key is the provider's own transaction id, carried on every payment and every settlement row. A payment is matched on `(provider_txn_id, kind)` where kind is `settled` or `refunded`; a refund shares its capture's id. Every difference gets one cause from a closed list (`timing`, `fee_discrepancy`, `missing_capture`, `duplicate_on_our_side`, `provider_only_refund`, `amount_mismatch`, and five that always go to a person) and each cause has one handling: carry, automatic, from the report, or human. The classification is a pure function in the domain layer, tested against each cause on its own.

## Alternatives considered

- **Fuzzy match on amount and timestamp.** Pairs unrelated transactions, so a clean result proves nothing.
- **Join on our own payment id.** The provider does not know it; the two ids differ.
- **One generic "mismatch" with a free-text reason.** Cannot drive an automatic action safely.

## Consequences

- A payment without a provider id cannot be reconciled; the column is `NOT NULL` and validated.
- `provider_txn_id` is deliberately not unique in `payments`: a second row for one provider transaction is the duplicate break, and the ledger under reconciliation must be able to hold it so the job can find it.
- A new cause means a new enum value, a new handling and a new test, and the compiler and the `HANDLING` test point at all three.
