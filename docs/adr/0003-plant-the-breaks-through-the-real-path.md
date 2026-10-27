# 3. Plant the breaks by their causes, through the real capture path

Status: accepted, 2026-10-27

## Context

The lesson plants six breaks in a day of 500 payments. Editing the CSV or the tables by hand would prove that the matcher finds edits, not that it finds what production produces, and it would not show that the ledger side genuinely comes from the capture code.

## Decision

The simulation drives every payment through `RecordCapture` against the stub provider, then makes the six things happen the way they happen: the 50th capture is made after the provider's cutoff (timing); the provider settles the 112th with a fee other than the one it quoted (fee); the process dies after the provider captured the 200th and before the ledger write, by an exception thrown between the two (missing capture); a retry with a new key records the 215th a second time (duplicate); the provider refunds the 300th and the service is never told (provider-only refund); the provider settles the 400th for 99.99 against the 95.00 captured (amount). The provider's settlement file is computed from the provider's own records. A seed makes the day repeatable.

## Alternatives considered

- **Hand-edited fixtures.** Proves less and drifts from the code.
- **Random faults.** Not repeatable; the lesson's ids are fixed.

## Consequences

- The clean day, the planted day and the next day come from the same code, so a bug in the capture path shows up in the reconciliation tests.
- The stub provider needs a settlement model (batches closed at a cutoff) and two adjustment hooks that a real provider has no API for; they stand for events the provider's own systems cause.
