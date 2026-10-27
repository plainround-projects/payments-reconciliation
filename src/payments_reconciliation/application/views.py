"""JSON-ready shapes for the API and the command-line program."""

from __future__ import annotations

from typing import Any

from payments_reconciliation.application.daily_report import DailyReport, Line, render
from payments_reconciliation.application.ports import RunRecord, StoredBreak
from payments_reconciliation.application.resolve_breaks import Resolution


def break_view(stored: StoredBreak) -> dict[str, Any]:
    """A stored break."""
    finding = stored.finding

    def minor(amount: Any) -> int | None:
        return None if amount is None else int(amount.minor)

    return {
        "id": stored.id,
        "run_date": stored.run_date.isoformat(),
        "provider_txn_id": finding.provider_txn_id,
        "kind": finding.kind.value,
        "cause": finding.cause.value,
        "handling": finding.handling.value,
        "status": stored.status.value,
        "ledger_amount_minor": minor(finding.ledger_amount),
        "settlement_amount_minor": minor(finding.settlement_amount),
        "ledger_fee_minor": minor(finding.ledger_fee),
        "settlement_fee_minor": minor(finding.settlement_fee),
        "detail": finding.detail,
        "note": stored.note,
    }


def run_view(run: RunRecord) -> dict[str, Any]:
    """A run's first-pass numbers."""
    return {
        "run_date": run.run_date.isoformat(),
        "currency": run.currency,
        "ledger_payments": run.ledger_payments,
        "settlement_rows": run.settlement_rows,
        "settled_total_minor": run.settled_total.minor,
        "refunded_total_minor": run.refunded_total.minor,
        "clean_transactions": run.clean_transactions,
        "clean_total_minor": run.clean_total.minor,
        "break_count": run.break_count,
    }


def resolution_view(resolution: Resolution) -> dict[str, Any]:
    """One resolution."""
    return {
        "provider_txn_id": resolution.stored.finding.provider_txn_id,
        "cause": resolution.stored.finding.cause.value,
        "outcome": resolution.outcome.value,
        "message": resolution.message,
    }


def _line(line: Line) -> dict[str, Any]:
    return {"provider_txn_id": line.provider_txn_id, "cause": line.cause, "text": line.text}


def report_view(report: DailyReport) -> dict[str, Any]:
    """The daily report as JSON, with its text rendering."""
    return {
        "business_date": report.business_date.isoformat(),
        "currency": report.currency,
        "run": run_view(report.run),
        "settled_rows": report.settled_rows,
        "settled_total_minor": report.settled_total.minor,
        "buckets_minor": {name: amount.minor for name, amount in report.buckets.items()},
        "balances": report.balances,
        "clean_transactions": report.clean_transactions,
        "auto_resolved": [_line(line) for line in report.auto_resolved],
        "resolved": [_line(line) for line in report.resolved],
        "carried": [_line(line) for line in report.carried],
        "settled_later": [_line(line) for line in report.settled_later],
        "cleared_carried": [_line(line) for line in report.cleared_carried],
        "worklist": [_line(line) for line in report.worklist],
        "open": [_line(line) for line in report.open_items],
        "ledger_difference_minor": report.ledger_difference.minor,
        "ledger_sum_minor": report.ledger_sum.minor,
        "text": render(report),
    }
