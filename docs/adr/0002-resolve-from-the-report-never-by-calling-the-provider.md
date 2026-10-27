# 2. Resolve breaks from the report's data, once, and never by calling the provider again

Status: accepted, 2026-10-27

## Context

For a missing capture or a refund the provider already made, the money has moved. Asking the provider to capture or refund again risks moving it twice. The daily job also runs again after a crash, and two operators may resolve at the same time.

## Decision

Each resolver writes to our ledger only, using the settlement row's data: a fee correction is one balanced entry for the difference between the recorded and the settled fee; a missing capture is inserted as a payment with the six capture rows; a duplicate is undone by reversing entries (the capture's legs, swapped) and marked `DUPLICATE_REVERSED`, never deleted; a provider-only refund is posted as the refund entries. Three things make a second run a no-op: the resolver looks at the ledger first and answers "already resolved"; each break is resolved under a row lock, so two workers queue; and the ledger's unique key on `(payment, operation, reference, leg)` refuses a second posting of the same fee correction, reversal or refund even if the first two fail. Resolution and status change commit together. Anything that needs a decision (an amount that differs, a timing difference that never settles, two settled rows for one id) goes to the worklist.

## Alternatives considered

- **Call the provider to fix it.** Risks a second real movement of money.
- **Delete the duplicate row.** Destroys the audit trail of what the ledger once claimed.
- **Edit the recorded fee.** Rewrites history; a correcting entry shows when and why it changed.

## Consequences

- The ledger holds every correction as entries; the trial balance still sums to zero after any number of passes (`make run` prints the ledger digest before and after the second pass).
- A reappearing break (someone puts the old fee back) is reopened, not ignored.
- A refund for a payment the ledger does not have cannot be posted; it goes to the worklist until the missing capture is resolved (resolvers run in that order).
