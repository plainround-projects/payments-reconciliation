"""Reconcile one business day: read the provider's file, match it against the ledger, classify and store the breaks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time

from payments_reconciliation.application.logs import fields
from payments_reconciliation.application.ports import (
    BreakStatus,
    Clock,
    RunRecord,
    SettlementFiles,
    StoredBreak,
    UnitOfWork,
    UnitOfWorkFactory,
)
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.reconciliation import Handling, LedgerView, MatchResult, reconcile
from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, parse_csv

logger = logging.getLogger(__name__)

_UNRESOLVED = {BreakStatus.OPEN, BreakStatus.NEEDS_REVIEW, BreakStatus.CARRIED}


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """What a run found, and the stored breaks it left."""

    run: RunRecord
    result: MatchResult
    breaks: list[StoredBreak]
    cleared_carried: list[StoredBreak]


class ReconcileDay:
    """Join the ledger and the settlement report of one day on the provider's transaction id.

    Running a day again is safe: breaks are keyed by (day, transaction, kind, cause), a resolved break stays
    resolved, and a break that no longer reproduces is cleared.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        files: SettlementFiles,
        clock: Clock,
        *,
        currency: str,
        cutoff: time,
        max_carry_days: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._files = files
        self._clock = clock
        self._currency = currency
        self._cutoff = cutoff
        self._max_carry_days = max_carry_days

    def execute(self, business_date: date) -> ReconcileOutcome:
        """Run the day. Raises `SettlementMissingError` when the provider's file has not arrived."""
        report = parse_csv(self._files.read(business_date), business_date, self._currency)
        with self._uow_factory() as uow:
            now = self._clock.now()
            carried = {stored.finding.provider_txn_id: stored for stored in uow.breaks.carried()}
            carried_days = {txn: (business_date - stored.run_date).days for txn, stored in carried.items()}
            ledger = self._ledger_view(uow, business_date, set(carried))
            ledger = self._without_settled_later(uow, business_date, ledger)
            result = reconcile(
                ledger, report, carried_days=carried_days, cutoff=self._cutoff, max_carry_days=self._max_carry_days
            )
            run = self._run_record(business_date, report, ledger, result, now)
            uow.runs.save(run)  # first: the breaks below refer to it
            cleared = self._clear_carried(uow, carried, result, business_date, now)
            self._store(uow, business_date, result, carried, now)
            stored_breaks = uow.breaks.for_run(business_date)
        logger.info(
            "reconciled",
            extra=fields(
                business_date=business_date.isoformat(), breaks=len(result.breaks), clean=run.clean_transactions
            ),
        )
        return ReconcileOutcome(run, result, stored_breaks, cleared)

    def _ledger_view(self, uow: UnitOfWork, business_date: date, carried_ids: set[str]) -> LedgerView:
        payments = {payment.id: payment for payment in uow.payments.captured_on(business_date)}
        payments.update({payment.id: payment for payment in uow.payments.by_txns(carried_ids)})
        refunds = uow.refunds.recorded_on(business_date, self._currency)
        return LedgerView(tuple(payments.values()), refunds)

    @staticmethod
    def _without_settled_later(uow: UnitOfWork, business_date: date, ledger: LedgerView) -> LedgerView:
        """Leave out payments whose timing difference already cleared in a later report, so a re-run stays quiet."""
        settled_later = {
            stored.finding.provider_txn_id
            for stored in uow.breaks.for_run(business_date)
            if stored.status is BreakStatus.CLEARED and stored.cleared_on is not None
        }
        kept = tuple(payment for payment in ledger.payments if payment.provider_txn_id not in settled_later)
        return LedgerView(kept, ledger.refunds)

    def _clear_carried(
        self, uow: UnitOfWork, carried: dict[str, StoredBreak], result: MatchResult, business_date: date, now: datetime
    ) -> list[StoredBreak]:
        matched_ids = {item.provider_txn_id for item in result.matched if item.kind is SettlementKind.SETTLED}
        cleared: list[StoredBreak] = []
        for txn_id, stored in carried.items():
            if txn_id in matched_ids and txn_id not in result.break_ids():
                note = f"settled in the report of {business_date.isoformat()}; no action was needed"
                uow.breaks.update(stored.id, BreakStatus.CLEARED, note, now, cleared_on=business_date)
                cleared.append(stored)
        return cleared

    def _store(
        self, uow: UnitOfWork, business_date: date, result: MatchResult, carried: dict[str, StoredBreak], now: datetime
    ) -> None:
        found_keys: set[tuple[str, SettlementKind, str]] = set()
        for finding in result.breaks:
            if finding.provider_txn_id in carried and finding.cause.value == "timing":
                waiting = carried[finding.provider_txn_id]
                uow.breaks.refresh(waiting.id, finding, now)  # still waiting: the original row keeps counting
                continue
            if finding.provider_txn_id in carried and finding.cause.value == "timing_overdue":
                waiting = carried[finding.provider_txn_id]
                uow.breaks.update(waiting.id, BreakStatus.ESCALATED, "waited too long; see the new break", now)
            found_keys.add((finding.provider_txn_id, finding.kind, finding.cause.value))
            existing = uow.breaks.find(business_date, finding.provider_txn_id, finding.kind, finding.cause.value)
            if existing is None:
                uow.breaks.insert(business_date, finding, self._initial_status(finding.handling), finding.detail, now)
            elif existing.status in _UNRESOLVED:
                uow.breaks.refresh(existing.id, finding, now)
            else:
                uow.breaks.update(existing.id, BreakStatus.OPEN, "reappeared after it was resolved", now)
                uow.breaks.refresh(existing.id, finding, now)
        for stored in uow.breaks.for_run(business_date):
            key = (stored.finding.provider_txn_id, stored.finding.kind, stored.finding.cause.value)
            if key not in found_keys and stored.status in {BreakStatus.OPEN, BreakStatus.NEEDS_REVIEW}:
                uow.breaks.update(stored.id, BreakStatus.CLEARED, "no longer differs on this run", now)

    @staticmethod
    def _initial_status(handling: Handling) -> BreakStatus:
        if handling is Handling.CARRY:
            return BreakStatus.CARRIED
        if handling is Handling.HUMAN:
            return BreakStatus.NEEDS_REVIEW
        return BreakStatus.OPEN

    def _run_record(
        self, business_date: date, report: SettlementReport, ledger: LedgerView, result: MatchResult, now: datetime
    ) -> RunRecord:
        broken = result.break_ids()
        clean = [
            item
            for item in result.matched
            if item.kind is SettlementKind.SETTLED and item.provider_txn_id not in broken
        ]
        clean_total = Money.zero(self._currency)
        for item in clean:
            clean_total = clean_total + item.amount
        return RunRecord(
            run_date=business_date,
            currency=self._currency,
            ledger_payments=sum(1 for payment in ledger.payments if payment.active),
            settlement_rows=len(report.rows),
            settled_total=report.settled_total,
            refunded_total=report.refunded_total,
            clean_transactions=len(clean),
            clean_total=clean_total,
            break_count=len(result.breaks),
            ran_at=now,
        )
