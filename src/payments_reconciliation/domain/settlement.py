"""The provider's settlement report: its rows, and reading and writing the CSV file."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from payments_reconciliation.domain.errors import ReconciliationError, SettlementFormatError
from payments_reconciliation.domain.money import Money

HEADER = ["provider_txn_id", "amount", "fee", "status", "settled_at"]
_TXN_ID = re.compile(r"^ptx_[A-Za-z0-9]{1,32}$")


class SettlementKind(StrEnum):
    """`settled` rows move money to the merchant; `refunded` rows take it back (negative amount)."""

    SETTLED = "settled"
    REFUNDED = "refunded"


@dataclass(frozen=True, slots=True)
class SettlementRow:
    """One line of the report. The provider's transaction id is the join key; a refund shares its capture's id."""

    provider_txn_id: str
    kind: SettlementKind
    amount: Money
    fee: Money
    settled_at: datetime

    def __post_init__(self) -> None:
        if not _TXN_ID.match(self.provider_txn_id):
            raise SettlementFormatError(f"not a provider transaction id: {self.provider_txn_id!r}")
        if self.settled_at.tzinfo is None:
            raise SettlementFormatError("settled_at must carry a timezone")
        if self.kind is SettlementKind.SETTLED and not self.amount.is_positive():
            raise SettlementFormatError(f"{self.provider_txn_id}: a settled row needs a positive amount")
        if self.kind is SettlementKind.REFUNDED and self.amount.minor >= 0:
            raise SettlementFormatError(f"{self.provider_txn_id}: a refunded row needs a negative amount")
        if self.fee.minor < 0:
            raise SettlementFormatError(f"{self.provider_txn_id}: the fee cannot be negative")


@dataclass(frozen=True, slots=True)
class SettlementReport:
    """All rows the provider settled for one business day."""

    business_date: date
    currency: str
    rows: tuple[SettlementRow, ...]

    def of_kind(self, kind: SettlementKind) -> tuple[SettlementRow, ...]:
        """The rows of one kind, in file order."""
        return tuple(row for row in self.rows if row.kind is kind)

    @property
    def settled_total(self) -> Money:
        """The sum of the `settled` rows."""
        total = Money.zero(self.currency)
        for row in self.of_kind(SettlementKind.SETTLED):
            total = total + row.amount
        return total

    @property
    def refunded_total(self) -> Money:
        """The sum of the `refunded` rows (zero or negative)."""
        total = Money.zero(self.currency)
        for row in self.of_kind(SettlementKind.REFUNDED):
            total = total + row.amount
        return total


def format_csv(rows: Iterable[SettlementRow]) -> str:
    """The provider's file: `provider_txn_id,amount,fee,status,settled_at`, amounts in decimal text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for row in rows:
        stamp = row.settled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        writer.writerow([row.provider_txn_id, row.amount.format(), row.fee.format(), row.kind.value, stamp])
    return buffer.getvalue()


def parse_csv(text: str, business_date: date, currency: str) -> SettlementReport:
    """Read the provider's file. Any malformed line is an error that names its line number; nothing is skipped."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as error:
        raise SettlementFormatError("the settlement file is empty") from error
    if header != HEADER:
        raise SettlementFormatError(f"expected the header {','.join(HEADER)}, got {','.join(header)}")
    rows: list[SettlementRow] = []
    for number, record in enumerate(reader, start=2):
        if not record:
            continue
        if len(record) != len(HEADER):
            raise SettlementFormatError(f"line {number}: expected {len(HEADER)} columns, got {len(record)}")
        txn_id, amount, fee, status, stamp = record
        try:
            rows.append(
                SettlementRow(
                    txn_id,
                    SettlementKind(status),
                    Money.parse(amount, currency),
                    Money.parse(fee, currency),
                    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
                )
            )
        except (ValueError, ReconciliationError) as error:
            raise SettlementFormatError(f"line {number}: {error}") from error
    return SettlementReport(business_date, currency, tuple(rows))
