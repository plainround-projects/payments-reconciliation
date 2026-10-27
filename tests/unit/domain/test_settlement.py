from datetime import UTC, date, datetime

import pytest

from payments_reconciliation.domain.errors import SettlementFormatError
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind, SettlementRow, format_csv, parse_csv

DAY = date(2026, 10, 20)
STAMP = datetime(2026, 10, 20, 23, 30, tzinfo=UTC)
HEADER = "provider_txn_id,amount,fee,status,settled_at\n"


def row(txn="ptx_0001", kind=SettlementKind.SETTLED, amount=4250, fee=123):
    return SettlementRow(txn, kind, Money(amount, "USD"), Money(fee, "USD"), STAMP)


def test_the_file_round_trips_in_the_providers_shape():
    rows = [row(), row("ptx_0002", amount=1800, fee=52), row("ptx_0002", SettlementKind.REFUNDED, -500, 0)]
    text = format_csv(rows)
    assert text.splitlines()[:2] == [
        "provider_txn_id,amount,fee,status,settled_at",
        "ptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z",
    ]
    assert "ptx_0002,-5.00,0.00,refunded,2026-10-20T23:30:00Z" in text
    assert parse_csv(text, DAY, "USD").rows == tuple(rows)


def test_totals_split_settled_money_from_refunds():
    report = parse_csv(
        format_csv([row(), row("ptx_0002", amount=1000), row("ptx_0002", SettlementKind.REFUNDED, -300, 0)]), DAY, "USD"
    )
    assert report.settled_total == Money(5250, "USD") and report.refunded_total == Money(-300, "USD")
    assert len(report.of_kind(SettlementKind.REFUNDED)) == 1


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "empty"),
        ("id,amount\n", "header"),
        (HEADER + "ptx_0001,42.50,1.23,settled\n", "columns"),
        (HEADER + "ptx_0001,abc,1.23,settled,2026-10-20T23:30:00Z\n", "amount"),
        (HEADER + "ptx_0001,42.505,1.23,settled,2026-10-20T23:30:00Z\n", "decimals"),
        (HEADER + "order-9,42.50,1.23,settled,2026-10-20T23:30:00Z\n", "id shape"),
        (HEADER + "ptx_0001,42.50,1.23,pending,2026-10-20T23:30:00Z\n", "status"),
        (HEADER + "ptx_0001,42.50,1.23,settled,yesterday\n", "timestamp"),
        (HEADER + "ptx_0001,-42.50,1.23,settled,2026-10-20T23:30:00Z\n", "negative settled"),
        (HEADER + "ptx_0001,42.50,0.00,refunded,2026-10-20T23:30:00Z\n", "positive refund"),
        (HEADER + "ptx_0001,42.50,-1.00,settled,2026-10-20T23:30:00Z\n", "negative fee"),
    ],
)
def test_a_malformed_file_is_refused_and_nothing_is_skipped(text, why):
    with pytest.raises(SettlementFormatError):
        parse_csv(text, DAY, "USD")


def test_the_error_names_the_line():
    with pytest.raises(SettlementFormatError, match="line 3"):
        parse_csv(
            HEADER + "ptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z\nptx_0002,x,0.00,settled,2026-10-20T23:30:00Z\n",
            DAY,
            "USD",
        )


def test_blank_lines_are_ignored():
    assert len(parse_csv(HEADER + "\nptx_0001,42.50,1.23,settled,2026-10-20T23:30:00Z\n\n", DAY, "USD").rows) == 1


def test_a_row_needs_a_timezone():
    with pytest.raises(SettlementFormatError):
        SettlementRow("ptx_0001", SettlementKind.SETTLED, Money(1, "USD"), Money(0, "USD"), datetime(2026, 10, 20))
