from datetime import UTC, date, datetime, time, timedelta

import pytest

from payments_reconciliation.domain.errors import PaymentNotFoundError, SettlementMissingError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind
from payments_reconciliation.infrastructure.files import DirectorySettlementFiles
from payments_reconciliation.infrastructure.settings import ConfigurationError, Settings
from payments_reconciliation.infrastructure.stub_provider import StubProvider

CUTOFF = time(23, 0)
DAY = date(2026, 10, 20)
USD = "USD"


def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def test_capture_is_idempotent_on_its_key_and_ids_are_sequential():
    provider = StubProvider(FeePolicy(290))
    first = provider.capture(idempotency_key="k1", amount=Money(4250, USD), occurred_at=at(9))
    again = provider.capture(idempotency_key="k1", amount=Money(4250, USD), occurred_at=at(9))
    second = provider.capture(idempotency_key="k2", amount=Money(1800, USD), occurred_at=at(10))
    assert first == again and (first.provider_txn_id, second.provider_txn_id) == ("ptx_0001", "ptx_0002")
    assert first.fee == Money(123, USD) and provider.executed["capture"] == 2


def test_a_new_series_restarts_the_numbering_with_its_prefix():
    provider = StubProvider(FeePolicy(290))
    provider.capture(idempotency_key="k1", amount=Money(100, USD), occurred_at=at(9))
    provider.start_series("ptx_n")
    assert (
        provider.capture(idempotency_key="k2", amount=Money(100, USD), occurred_at=at(9)).provider_txn_id == "ptx_n0001"
    )


def test_the_batch_holds_what_was_captured_since_the_previous_cutoff():
    provider = StubProvider(FeePolicy(290))
    provider.capture(idempotency_key="before", amount=Money(1000, USD), occurred_at=at(12))
    provider.capture(idempotency_key="late", amount=Money(2000, USD), occurred_at=at(23, 40))
    today = provider.settlement_rows(DAY, CUTOFF)
    tomorrow = provider.settlement_rows(DAY + timedelta(days=1), CUTOFF)
    assert [row.provider_txn_id for row in today] == ["ptx_0001"]
    assert [row.provider_txn_id for row in tomorrow] == ["ptx_0002"]
    assert today[0].settled_at == at(23, 30)


def test_adjustments_change_what_is_settled_not_what_was_captured():
    provider = StubProvider(FeePolicy(290))
    result = provider.capture(idempotency_key="k", amount=Money(4250, USD), occurred_at=at(12))
    provider.adjust_fee(result.provider_txn_id, Money(141, USD))
    provider.adjust_amount(result.provider_txn_id, Money(4300, USD))
    [row] = provider.settlement_rows(DAY, CUTOFF)
    assert (row.amount, row.fee) == (Money(4300, USD), Money(141, USD)) and result.fee == Money(123, USD)


def test_a_refund_shows_as_a_negative_row_with_the_same_transaction_id():
    provider = StubProvider(FeePolicy(290))
    result = provider.capture(idempotency_key="k", amount=Money(2500, USD), occurred_at=at(12))
    first = provider.refund(
        idempotency_key="r", provider_txn_id=result.provider_txn_id, amount=Money(2500, USD), occurred_at=at(13)
    )
    again = provider.refund(
        idempotency_key="r", provider_txn_id=result.provider_txn_id, amount=Money(2500, USD), occurred_at=at(13)
    )
    rows = provider.settlement_rows(DAY, CUTOFF)
    assert first == again and provider.executed["refund"] == 1
    assert [(row.provider_txn_id, row.kind, row.amount.minor) for row in rows] == [
        ("ptx_0001", SettlementKind.REFUNDED, -2500),
        ("ptx_0001", SettlementKind.SETTLED, 2500),
    ]


def test_a_refund_for_an_unknown_transaction_or_over_the_capture_is_refused():
    provider = StubProvider(FeePolicy(290))
    with pytest.raises(PaymentNotFoundError):
        provider.refund(idempotency_key="r", provider_txn_id="ptx_9999", amount=Money(1, USD), occurred_at=at(12))
    result = provider.capture(idempotency_key="k", amount=Money(100, USD), occurred_at=at(12))
    with pytest.raises(PaymentNotFoundError):
        provider.refund(
            idempotency_key="r2", provider_txn_id=result.provider_txn_id, amount=Money(101, USD), occurred_at=at(12)
        )


def test_files_are_written_whole_and_read_back(tmp_path):
    files = DirectorySettlementFiles(tmp_path / "nested")
    files.write(DAY, "a,b\n")
    assert files.read(DAY) == "a,b\n" and files.path_for(DAY).name == "settlement_2026-10-20.csv"
    assert [p.name for p in (tmp_path / "nested").iterdir()] == ["settlement_2026-10-20.csv"]
    files.write(DAY, "c,d\n")
    assert files.read(DAY) == "c,d\n"


def test_a_missing_file_is_a_named_error(tmp_path):
    with pytest.raises(SettlementMissingError, match="2026-10-20"):
        DirectorySettlementFiles(tmp_path).read(DAY)


def test_defaults_give_a_working_local_configuration():
    settings = Settings.from_env({"POSTGRES_PASSWORD": "not-a-real-secret"})
    assert settings.postgres.port == 54612 and settings.api.port == 8462
    assert settings.reconciliation.cutoff == time(23, 0) and settings.reconciliation.max_carry_days == 2
    assert settings.reconciliation.currency == "USD" and "not-a-real-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POSTGRES_PORT", "0"),
        ("SETTLEMENT_CUTOFF_UTC", "25:61"),
        ("SETTLEMENT_CUTOFF_UTC", "late"),
        ("CURRENCY", "XXX"),
        ("MAX_CARRY_DAYS", "0"),
        ("PROVIDER_FEE_BPS", "10001"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_a_bad_value_names_the_variable(name, value):
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({"POSTGRES_PASSWORD": "x", name: value})


def test_the_password_has_no_default():
    with pytest.raises(ConfigurationError, match="POSTGRES_PASSWORD"):
        Settings.from_env({})
