# 4. A timing difference is a cutoff, a carry and a limit

Status: accepted, 2026-10-27

## Context

A transaction captured late in the day is not in today's report and is in tomorrow's. That is not an error, but a transaction that never arrives is. The job has to tell them apart without a person, and must not count the first as a shortfall in today's total.

## Decision

The provider closes its batch at a cutoff (23:00 UTC by default). A ledger payment missing from the report, captured at or after the cutoff on the business day, is a `timing` break with status `CARRIED`. It is carried: the next day's ledger view includes carried transactions, so when the next report contains it, it matches, the carried break becomes `CLEARED` with the date it cleared, and nobody acted. If it is still missing after `MAX_CARRY_DAYS` days, it becomes `timing_overdue` on the worklist and the old row is `ESCALATED`. A ledger payment missing from the report that was captured before the cutoff is `ledger_only_unexplained` and goes to a person at once. The report's balance proof leaves carried transactions out of the day's settled total, because they are not in the file. Re-running a day whose timing difference has since settled leaves that payment out of the comparison, so the re-run stays quiet.

## Alternatives considered

- **Match any ledger-only payment later.** Hides payments that will never settle.
- **A fixed one-day grace.** Does not cover weekends or a provider that settles in two days; the limit is configuration.

## Consequences

- The cutoff and the carry limit are settings; the report says which transactions waited and which cleared.
- The rule assumes the provider's batch boundary is known and stable; if it moves, `SETTLEMENT_CUTOFF_UTC` must move with it.
