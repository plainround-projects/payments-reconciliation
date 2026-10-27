"""`payments-reconciliation`: simulate a day, reconcile it, resolve its breaks, print the report, run the scenario."""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable, Sequence
from datetime import date

from payments_reconciliation.application.daily_report import render
from payments_reconciliation.application.ports import BreakStatus, StoredBreak
from payments_reconciliation.application.resolve_breaks import Outcome, Resolution
from payments_reconciliation.domain.errors import InvalidDateError, ReconciliationError
from payments_reconciliation.domain.reconciliation import Cause
from payments_reconciliation.infrastructure.logging import configure_logging
from payments_reconciliation.infrastructure.settings import ConfigurationError, Settings
from payments_reconciliation.infrastructure.simulation import DayPlan
from payments_reconciliation.interfaces.container import Container, build_container

CLEAN_DAY = date(2026, 10, 18)
PLANTED_DAY = date(2026, 10, 20)
NEXT_DAY = date(2026, 10, 21)

# The lesson's six transactions and the cause each must be classified as.
EXPECTED_CAUSES = {
    "ptx_0050": Cause.TIMING,
    "ptx_0112": Cause.FEE_DISCREPANCY,
    "ptx_0200": Cause.MISSING_CAPTURE,
    "ptx_0215": Cause.DUPLICATE_ON_OUR_SIDE,
    "ptx_0300": Cause.PROVIDER_ONLY_REFUND,
    "ptx_0400": Cause.AMOUNT_MISMATCH,
}


class ScenarioError(AssertionError):
    """The system did something the lesson says it must not."""


def expect(condition: bool, message: str) -> None:
    """Raise `ScenarioError` with `message` unless `condition` holds."""
    if not condition:
        raise ScenarioError(message)


def parse_date(text: str) -> date:
    """An ISO date, or `InvalidDateError`."""
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise InvalidDateError(f"{text!r} is not a date; use YYYY-MM-DD") from error


def _ledger_state(container: Container) -> tuple[str, str]:
    with container.uow_factory() as uow:
        return uow.ledger.digest(), uow.ledger.total("USD").format()


def cmd_simulate(container: Container, args: argparse.Namespace) -> int:
    """Run a simulated day through the capture path and write the provider's settlement file."""
    plan = DayPlan(args.date, args.payments, args.seed, args.prefix, plant_breaks=args.plant)
    result = container.simulator.run(plan)
    print(
        f"simulated {result.business_date}: {result.recorded} payments recorded in the ledger, "
        f"{result.settlement_rows} rows in the settlement file ({result.seconds:.1f}s)"
    )
    if result.crashed_after_provider:
        print(f"  the process died after the provider captured: {', '.join(result.crashed_after_provider)}")
    if result.duplicated:
        print(f"  a retry recorded the same transaction twice: {', '.join(result.duplicated)}")
    if result.refunded_at_provider_only:
        print(f"  the provider refunded, the service never heard: {', '.join(result.refunded_at_provider_only)}")
    print(f"  settlement file: {container.files.path_for(args.date)}")
    return 0


def _print_breaks(breaks: list[StoredBreak]) -> None:
    print(f"breaks found: {len(breaks)}")
    for stored in breaks:
        print(f"  {stored.finding.provider_txn_id}  {stored.finding.cause.value}")


def cmd_reconcile(container: Container, args: argparse.Namespace) -> int:
    """Reconcile one day and list its breaks."""
    outcome = container.reconcile.execute(args.date)
    run = outcome.run
    print(
        f"{args.date}: {run.settlement_rows} settlement rows, {run.ledger_payments} payments in the ledger, "
        f"{run.clean_transactions} transactions matched cleanly"
    )
    if not outcome.breaks:
        print("0 breaks found")
    else:
        _print_breaks(outcome.breaks)
    for cleared in outcome.cleared_carried:
        print(f"  {cleared.finding.provider_txn_id}: timing difference settled, carried since {cleared.run_date}")
    return 0


def _resolution_line(resolution: Resolution) -> str:
    return f"{resolution.stored.finding.provider_txn_id}: {resolution.message or resolution.outcome.value}"


def cmd_resolve(container: Container, args: argparse.Namespace) -> int:
    """Resolve the breaks of one day that can be resolved safely."""
    for resolution in container.resolve.execute(args.date):
        print(_resolution_line(resolution))
    return 0


def cmd_report(container: Container, args: argparse.Namespace) -> int:
    """Print the daily report."""
    print(render(container.report.execute(args.date)))
    return 0


def cmd_worklist(container: Container, args: argparse.Namespace) -> int:
    """Print the breaks that need a person."""
    items = container.worklist.execute()
    if not items:
        print("the worklist is empty")
    for stored in items:
        reason = stored.note or stored.finding.detail
        print(f"{stored.run_date}  {stored.finding.provider_txn_id}  {stored.finding.cause.value}  {reason}")
    return 0


def cmd_demo(container: Container, args: argparse.Namespace) -> int:
    """The lesson's scenario: a clean day, the planted day, resolution twice, the report, and the next day."""
    payments = args.payments
    print(f"== 1. A clean day ({CLEAN_DAY}): {payments} payments, nothing planted")
    cmd_simulate(container, _namespace(CLEAN_DAY, payments, 42, "ptx_a", False))
    outcome = container.reconcile.execute(CLEAN_DAY)
    expect(not outcome.breaks, f"a clean day must reconcile with zero breaks, found {len(outcome.breaks)}")
    expect(outcome.run.clean_transactions == payments, "every transaction must match on its provider id")
    matched = outcome.run.clean_transactions
    print(f"reconcile: {matched} of {payments} matched on the provider's transaction id, 0 breaks found")
    print()
    print(f"== 2. The planted day ({PLANTED_DAY}): {payments} payments and six planted breaks")
    cmd_simulate(container, _namespace(PLANTED_DAY, payments, 43, "ptx_", True))
    outcome = container.reconcile.execute(PLANTED_DAY)
    _print_breaks(outcome.breaks)
    found = {stored.finding.provider_txn_id: stored.finding.cause for stored in outcome.breaks}
    expect(found == EXPECTED_CAUSES, f"expected {EXPECTED_CAUSES}, found {found}")
    print(f"{len(found)} breaks found, {len(found)} classified (each with its own cause)")
    print()
    print("== 3. Resolve: rules for the benign ones, the report's data for the real ones")
    for resolution in container.resolve.execute(PLANTED_DAY):
        print(_resolution_line(resolution))
    print()
    print("== 4. Run every resolver a second time")
    before = _ledger_state(container)
    second = container.resolve.execute(PLANTED_DAY)
    after = _ledger_state(container)
    print(
        "   ".join(
            f"{item.stored.finding.provider_txn_id}: {item.outcome.value}"
            for item in second
            if item.outcome is Outcome.ALREADY_RESOLVED
        )
    )
    expect(before == after, "the second pass must not change the ledger")
    expect(all(item.outcome is not Outcome.RESOLVED for item in second), "nothing may be resolved twice")
    print(f"(ledger digest before {before[0][:12]}, after {after[0][:12]}: identical; ledger sum {after[1]})")
    print()
    print("== 5. Reconcile the day again: only the real break is left")
    again = container.reconcile.execute(PLANTED_DAY)
    open_statuses = {
        stored.finding.provider_txn_id: stored.status
        for stored in again.breaks
        if stored.status in {BreakStatus.NEEDS_REVIEW, BreakStatus.OPEN, BreakStatus.CARRIED}
    }
    print(f"still unresolved: {', '.join(f'{k} ({v.value})' for k, v in sorted(open_statuses.items()))}")
    expect(
        set(open_statuses) == {"ptx_0050", "ptx_0400"}, f"expected ptx_0050 (carried) and ptx_0400, got {open_statuses}"
    )
    print()
    print("== 6. The daily report")
    report = container.report.execute(PLANTED_DAY)
    print(render(report))
    expect(report.balances, "the report's buckets must add up to the settlement report's settled total")
    expect([line.provider_txn_id for line in report.worklist] == ["ptx_0400"], "the worklist must hold ptx_0400 only")
    print()
    print(f"== 7. The next day ({NEXT_DAY}): ptx_0050 settles and clears itself")
    cmd_simulate(container, _namespace(NEXT_DAY, 30, 44, "ptx_n", False))
    next_outcome = container.reconcile.execute(NEXT_DAY)
    cleared = [item.finding.provider_txn_id for item in next_outcome.cleared_carried]
    print(f"cleared carried differences: {', '.join(cleared) or 'none'}")
    expect(cleared == ["ptx_0050"], f"ptx_0050 must clear with no action, got {cleared}")
    expect(not next_outcome.breaks, f"the next day must have no breaks, found {len(next_outcome.breaks)}")
    print("worklist after the next day:")
    cmd_worklist(container, args)
    final = container.worklist.execute()
    expect([item.finding.provider_txn_id for item in final] == ["ptx_0400"], "only ptx_0400 needs a human")
    with container.uow_factory() as uow:
        total = uow.ledger.total("USD")
    expect(total.is_zero(), f"the ledger must sum to zero, got {total.format()}")
    print(f"ledger sum across every posting: {total.format()}")
    return 0


def _namespace(day: date, payments: int, seed: int, prefix: str, plant: bool) -> argparse.Namespace:
    return argparse.Namespace(date=day, payments=payments, seed=seed, prefix=prefix, plant=plant)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="payments-reconciliation", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    simulate = commands.add_parser("simulate-day", help="run a simulated day through the capture path")
    simulate.add_argument("--date", type=parse_date, required=True)
    simulate.add_argument("--payments", type=int, default=500)
    simulate.add_argument("--seed", type=int, default=42)
    simulate.add_argument("--prefix", default="ptx_", help="provider transaction id prefix (default ptx_)")
    simulate.add_argument("--plant", action="store_true", help="plant the lesson's six breaks")
    for name, help_text in (
        ("reconcile", "match the ledger against the day's settlement file"),
        ("resolve", "resolve the day's breaks that can be resolved safely"),
        ("report", "print the day's report"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--date", type=parse_date, required=True)
    commands.add_parser("worklist", help="list the breaks that need a person")
    demo = commands.add_parser("demo", help="the lesson's scenario, end to end")
    demo.add_argument("--payments", type=int, default=500)
    return parser


HANDLERS: dict[str, Callable[[Container, argparse.Namespace], int]] = {
    "simulate-day": cmd_simulate,
    "reconcile": cmd_reconcile,
    "resolve": cmd_resolve,
    "report": cmd_report,
    "worklist": cmd_worklist,
    "demo": cmd_demo,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command; exit 0 on success, 1 when the system broke a rule or the input is wrong, 2 for configuration."""
    args = _parser().parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        print(f"configuration error: {error}")
        return 2
    configure_logging("WARNING" if args.command == "demo" else settings.log_level)
    container = build_container(settings)
    try:
        return HANDLERS[args.command](container, args)
    except (ReconciliationError, ScenarioError) as error:
        print(f"FAILED: {error}")
        return 1
    finally:
        container.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
