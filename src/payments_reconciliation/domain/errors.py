"""Named errors of the domain. Each carries a stable `code` that clients and logs can rely on."""

from __future__ import annotations


class ReconciliationError(Exception):
    """Base of every error this service raises on purpose."""

    code = "reconciliation_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidMoneyError(ReconciliationError):
    """An amount or currency that cannot be represented."""

    code = "invalid_money"


class CurrencyMismatchError(ReconciliationError):
    """Two amounts in different currencies were combined."""

    code = "currency_mismatch"


class UnbalancedEntryError(ReconciliationError):
    """A journal entry that does not have one debit and one equal credit on different accounts."""

    code = "unbalanced_entry"


class SettlementFormatError(ReconciliationError):
    """A settlement file or row that does not have the provider's shape."""

    code = "settlement_format"


class SettlementMissingError(ReconciliationError):
    """No settlement report exists for the business day."""

    code = "settlement_missing"


class PaymentNotFoundError(ReconciliationError):
    """No payment has this provider transaction id."""

    code = "payment_not_found"


class InvalidDateError(ReconciliationError):
    """The business date is not an ISO date (YYYY-MM-DD)."""

    code = "invalid_date"
