"""The daily report: what matched, what rules cleared, what was fixed from the report, and what needs a person."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from payments_reconciliation.application.ports import (
    BreakStatus,
    Clock,
    RunRecord,
    SettlementFiles,
    StoredBreak,
    UnitOfWorkFactory,
)
from payments_reconciliation.domain.errors import ReconciliationError
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind, SettlementRow, parse_csv


class ReportNotAvailableError(ReconciliationError):
    """The day has not been reconciled yet."""

    code = "report_not_available"


@dataclass(frozen=True, slots=True)
class Line:
    """One transaction in a report section."""

    provider_txn_id: str
    cause: str
    text: str
    amount_minor: int


@dataclass(frozen=True, slots=True)
class DailyReport:
    """The report for one business day. `buckets` partition the settled total, so the sum can be checked."""

    business_date: date
    currency: str
    run: RunRecord
    settled_rows: int
    settled_total: Money
    refunded_total: Money
    buckets: dict[str, Money]
    clean_transactions: int
    ledger_difference: Money
    ledger_sum: Money
    auto_resolved: list[Line] = field(default_factory=list)
    resolved: list[Line] = field(default_factory=list)
    carried: list[Line] = field(default_factory=list)
    settled_later: list[Line] = field(default_factory=list)
    cleared_carried: list[Line] = field(default_factory=list)
    worklist: list[Line] = field(default_factory=list)
    open_items: list[Line] = field(default_factory=list)

    @property
    def balances(self) -> bool:
        """True when the buckets add up to the settlement report's settled total."""
        total = Money.zero(self.currency)
        for amount in self.buckets.values():
            total = total + amount
        return total == self.settled_total


class BuildDailyReport:
    """Assemble the report for a reconciled day from its run, its breaks and the provider's file."""

    def __init__(self, uow_factory: UnitOfWorkFactory, files: SettlementFiles, clock: Clock, currency: str) -> None:
        self._uow_factory = uow_factory
        self._files = files
        self._clock = clock
        self._currency = currency

    def execute(self, business_date: date) -> DailyReport:
        """Raises `ReportNotAvailableError` when the day has no run."""
        report = parse_csv(self._files.read(business_date), business_date, self._currency)
        with self._uow_factory() as uow:
            run = uow.runs.get(business_date)
            if run is None:
                raise ReportNotAvailableError(f"{business_date.isoformat()} has not been reconciled")
            breaks = uow.breaks.for_run(business_date)
            cleared = uow.breaks.cleared_on(business_date)
            ledger_by_txn: dict[str, Money] = {}
            for payment in uow.payments.by_txns({row.provider_txn_id for row in report.rows}):
                if payment.active:
                    previous = ledger_by_txn.get(payment.provider_txn_id, Money.zero(self._currency))
                    ledger_by_txn[payment.provider_txn_id] = previous + payment.amount
            ledger_sum = uow.ledger.total(self._currency)
        return self._build(run, report.rows, breaks, cleared, ledger_by_txn, ledger_sum)

    def _build(
        self,
        run: RunRecord,
        rows: tuple[SettlementRow, ...],
        breaks: list[StoredBreak],
        cleared: list[StoredBreak],
        ledger_by_txn: dict[str, Money],
        ledger_sum: Money,
    ) -> DailyReport:
        settled = {row.provider_txn_id: row.amount for row in rows if row.kind is SettlementKind.SETTLED}
        status_of: dict[str, BreakStatus] = {}
        lines: dict[BreakStatus, list[Line]] = {status: [] for status in BreakStatus}
        for stored in breaks:
            finding = stored.finding
            line = Line(finding.provider_txn_id, finding.cause.value, stored.note or finding.detail, 0)
            lines[stored.status].append(line)
            current = status_of.get(finding.provider_txn_id)
            status_of[finding.provider_txn_id] = _worse(current, stored.status)
        cleared_ids = {stored.finding.provider_txn_id for stored in cleared}
        names = {
            BreakStatus.AUTO_RESOLVED: "auto_resolved",
            BreakStatus.RESOLVED: "resolved",
            BreakStatus.NEEDS_REVIEW: "worklist",
            BreakStatus.OPEN: "open",
            BreakStatus.CLEARED: "resolved",
            BreakStatus.CARRIED: "carried",
            BreakStatus.ESCALATED: "worklist",
        }
        buckets = {
            name: Money.zero(self._currency)
            for name in ("clean", "cleared_carried", "auto_resolved", "resolved", "worklist", "open")
        }
        for txn_id, amount in settled.items():
            if txn_id in cleared_ids:
                bucket = "cleared_carried"
            elif txn_id in status_of:
                bucket = names[status_of[txn_id]]
            else:
                bucket = "clean"
            buckets[bucket] = buckets[bucket] + amount
        difference = Money.zero(self._currency)
        for txn_id, amount in settled.items():
            difference = difference + (amount - ledger_by_txn.get(txn_id, Money.zero(self._currency)))
        return DailyReport(
            business_date=run.run_date,
            currency=self._currency,
            run=run,
            settled_rows=sum(1 for row in rows if row.kind is SettlementKind.SETTLED),
            settled_total=run.settled_total,
            refunded_total=run.refunded_total,
            buckets=buckets,
            clean_transactions=run.clean_transactions,
            auto_resolved=lines[BreakStatus.AUTO_RESOLVED],
            resolved=lines[BreakStatus.RESOLVED],
            carried=lines[BreakStatus.CARRIED],
            settled_later=[
                Line(stored.finding.provider_txn_id, stored.finding.cause.value, stored.note, 0)
                for stored in breaks
                if stored.status is BreakStatus.CLEARED and stored.cleared_on is not None
            ],
            cleared_carried=[
                Line(stored.finding.provider_txn_id, stored.finding.cause.value, stored.note, 0) for stored in cleared
            ],
            worklist=lines[BreakStatus.NEEDS_REVIEW],
            open_items=lines[BreakStatus.OPEN],
            ledger_difference=difference,
            ledger_sum=ledger_sum,
        )


_SEVERITY = [
    BreakStatus.NEEDS_REVIEW,
    BreakStatus.ESCALATED,
    BreakStatus.OPEN,
    BreakStatus.RESOLVED,
    BreakStatus.AUTO_RESOLVED,
    BreakStatus.CLEARED,
    BreakStatus.CARRIED,
]


def _worse(current: BreakStatus | None, new: BreakStatus) -> BreakStatus:
    if current is None:
        return new
    return current if _SEVERITY.index(current) <= _SEVERITY.index(new) else new


def render(report: DailyReport) -> str:
    """The report as text, in the shape the lesson shows."""
    day = report.business_date.isoformat()

    def money(amount: Money) -> str:
        return f"{amount.format()} {report.currency}"

    out = [
        f"Daily reconciliation report for {day}",
        f"Settlement report: {report.settled_rows} settled rows totalling {money(report.settled_total)}, "
        f"refunds {money(report.refunded_total)}",
        f"Matched at the first pass: {report.clean_transactions} transactions, {money(report.run.clean_total)}",
    ]
    out.append("Auto-resolved: " + _join(report.auto_resolved, lambda x: f"{x.provider_txn_id} ({x.cause})"))
    out.append("Resolved from the report: " + _join(report.resolved, lambda x: f"{x.provider_txn_id} ({x.cause})"))
    out.append("Carried to the next day: " + _join(report.carried, lambda x: f"{x.provider_txn_id} ({x.cause})"))
    if report.settled_later:
        out.append(
            "Carried and settled later: " + _join(report.settled_later, lambda x: f"{x.provider_txn_id} ({x.text})")
        )
    if report.cleared_carried:
        out.append("Cleared today (carried earlier): " + _join(report.cleared_carried, lambda x: x.provider_txn_id))
    if report.open_items:
        out.append(
            "Still open (run resolve): " + _join(report.open_items, lambda x: f"{x.provider_txn_id} ({x.cause})")
        )
    out.append(
        "Worklist (needs a human): " + _join(report.worklist, lambda x: f"{x.provider_txn_id} ({x.cause}: {x.text})")
    )
    parts = " + ".join(
        f"{name.replace('_', ' ')} {money(amount)}" for name, amount in report.buckets.items() if not amount.is_zero()
    )
    out.append(
        f"Balance: settled total {money(report.settled_total)} = {parts}  "
        f"[{'balances' if report.balances else 'DOES NOT BALANCE'}]"
    )
    out.append(f"Difference against the ledger for these transactions: {money(report.ledger_difference)}")
    out.append(f"Ledger sum across every posting: {money(report.ledger_sum)}")
    return "\n".join(out)


def _join(lines: list[Line], show: Callable[[Line], str]) -> str:
    return ", ".join(show(line) for line in lines) if lines else "none"
