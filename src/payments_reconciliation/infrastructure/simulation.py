"""A simulated day of payments, run through the real capture path, with the lesson's six breaks planted by cause.

Nothing is faked in the ledger or in the file. The breaks happen the way they happen in production:

- the **timing** difference is a capture made after the provider's cutoff;
- the **fee** difference is the provider settling with a fee other than the one it quoted at capture;
- the **missing capture** is a process that dies after the provider captured and before the ledger was written;
- the **duplicate** is a retry that recorded the same provider transaction a second time;
- the **provider-only refund** is a refund the provider made that the service never heard about;
- the **amount mismatch** is the provider settling an amount other than the one it captured.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic

from payments_reconciliation.application.ledger_ops import record_known_capture
from payments_reconciliation.application.ports import (
    CaptureResult,
    IdSource,
    SettlementFiles,
    UnitOfWorkFactory,
)
from payments_reconciliation.application.record_capture import RecordCapture
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import PaymentSource
from payments_reconciliation.domain.settlement import format_csv
from payments_reconciliation.infrastructure.stub_provider import StubProvider


class SimulatedCrashError(RuntimeError):
    """The process died between the provider's answer and the ledger write."""


class SimulatedClock:
    """A clock the simulation sets to each payment's time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def set(self, moment: datetime) -> None:
        """Move the clock."""
        self._now = moment

    def now(self) -> datetime:
        """The simulated time."""
        return self._now


@dataclass(frozen=True, slots=True)
class Plants:
    """Where, by position in the day's capture order, each break is planted (the lesson's ids ptx_0050 and so on)."""

    timing: int = 50
    fee: int = 112
    missing_capture: int = 200
    duplicate: int = 215
    refund: int = 300
    amount_mismatch: int = 400
    late_capture_time: time = time(23, 40)
    fee_at_settlement_minor: int = 141
    settled_amount_minor: int = 9999

    def amounts(self) -> dict[int, int]:
        """The captured amount at each planted position: the fee example is the lesson's 42.50, the mismatch 95.00."""
        return {
            self.timing: 5000,
            self.fee: 4250,
            self.missing_capture: 6000,
            self.duplicate: 3300,
            self.refund: 2500,
            self.amount_mismatch: 9500,
        }


# The lesson's worked examples sit at fixed positions on every day.
FIXED_AMOUNTS = {1: 4250, 2: 1800}


@dataclass(frozen=True, slots=True)
class DayPlan:
    """One simulated day."""

    business_date: date
    payments: int
    seed: int
    id_prefix: str = "ptx_"
    plant_breaks: bool = False
    plants: Plants = field(default_factory=Plants)


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """What the simulation did, for the program to print."""

    business_date: date
    recorded: int
    crashed_after_provider: list[str]
    duplicated: list[str]
    refunded_at_provider_only: list[str]
    settlement_rows: int
    seconds: float


class DaySimulator:
    """Run a `DayPlan` through `RecordCapture` and publish the provider's settlement file for the day."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        provider: StubProvider,
        clock: SimulatedClock,
        ids: IdSource,
        files: SettlementFiles,
        *,
        cutoff: time,
        currency: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = provider
        self._clock = clock
        self._ids = ids
        self._files = files
        self._cutoff = cutoff
        self._currency = currency
        self._crash_armed = False

    def run(self, plan: DayPlan) -> SimulationResult:
        """Capture `plan.payments` payments through the real path, plant the breaks, write the day's file."""
        started = monotonic()
        self._provider.start_series(plan.id_prefix)
        capture = RecordCapture(self._uow_factory, self._provider, self._clock, self._ids, self._maybe_crash)
        rng = random.Random(plan.seed)
        crashed: list[str] = []
        duplicated: list[str] = []
        refunded: list[str] = []
        recorded = 0
        for position in range(1, plan.payments + 1):
            moment = self._time_of(plan, position, rng)
            self._clock.set(moment)
            amount = Money(self._amount_of(plan, position, rng), self._currency)
            key = f"capture-{plan.business_date.isoformat()}-{plan.id_prefix}{position:04d}"
            planted = plan.plant_breaks
            self._crash_armed = planted and position == plan.plants.missing_capture
            try:
                payment = capture.execute(key, amount)
            except SimulatedCrashError:
                crashed.append(f"{plan.id_prefix}{position:04d}")
                continue
            finally:
                self._crash_armed = False
            recorded += 1
            if planted:
                self._plant_after_capture(plan, position, payment.provider_txn_id, amount, moment, duplicated, refunded)
        rows = self._provider.settlement_rows(plan.business_date, self._cutoff)
        self._files.write(plan.business_date, format_csv(rows))
        return SimulationResult(
            plan.business_date, recorded, crashed, duplicated, refunded, len(rows), monotonic() - started
        )

    def _maybe_crash(self, result: CaptureResult) -> None:
        if self._crash_armed:
            raise SimulatedCrashError(f"the process died after the provider captured {result.provider_txn_id}")

    def _plant_after_capture(
        self,
        plan: DayPlan,
        position: int,
        txn_id: str,
        amount: Money,
        moment: datetime,
        duplicated: list[str],
        refunded: list[str],
    ) -> None:
        plants = plan.plants
        if position == plants.fee:
            self._provider.adjust_fee(txn_id, Money(plants.fee_at_settlement_minor, self._currency))
        elif position == plants.amount_mismatch:
            self._provider.adjust_amount(txn_id, Money(plants.settled_amount_minor, self._currency))
        elif position == plants.refund:
            self._provider.refund(
                idempotency_key=f"refund-{txn_id}",
                provider_txn_id=txn_id,
                amount=amount,
                occurred_at=moment + timedelta(hours=1),
            )
            refunded.append(txn_id)  # the provider refunded; the service was never told
        elif position == plants.duplicate:
            self._record_again(txn_id, amount, moment + timedelta(seconds=3))
            duplicated.append(txn_id)

    def _record_again(self, txn_id: str, amount: Money, moment: datetime) -> None:
        """A retry with a brand-new key records the same provider transaction a second time."""
        with self._uow_factory() as uow:
            original = uow.payments.for_txn(txn_id)[0]
            record_known_capture(
                uow,
                payment_id=self._ids.new_id(),
                provider_txn_id=txn_id,
                amount=amount,
                fee=original.fee,
                captured_at=moment,
                source=PaymentSource.SERVICE_RETRY,
                idempotency_key=f"retry-{uuid.uuid4().hex[:12]}",
            )

    def _time_of(self, plan: DayPlan, position: int, rng: random.Random) -> datetime:
        if plan.plant_breaks and position == plan.plants.timing:
            return datetime.combine(plan.business_date, plan.plants.late_capture_time, tzinfo=UTC)
        start, end = 8 * 3600, 22 * 3600 + 30 * 60  # 08:00 to 22:30, before the cutoff
        step = (end - start) / max(plan.payments, 1)
        seconds = int(start + (position - 1) * step + rng.uniform(0, step * 0.9))
        return datetime.combine(plan.business_date, time(0, 0), tzinfo=UTC) + timedelta(seconds=seconds)

    @staticmethod
    def _amount_of(plan: DayPlan, position: int, rng: random.Random) -> int:
        drawn = rng.randint(500, 20_000)  # always drawn, so the stream does not depend on which positions are fixed
        if plan.plant_breaks:
            planted = plan.plants.amounts().get(position)
            if planted is not None:
                return planted
        return FIXED_AMOUNTS.get(position, drawn)
