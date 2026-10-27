"""The chart of accounts. Sign convention of every posting: positive is a debit, negative is a credit."""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Which side increases an account."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class Account(StrEnum):
    """The four accounts of the lesson. The value is the account code stored in the database."""

    CUSTOMER_RECEIVABLE = "customer_receivable"
    MERCHANT_PENDING = "merchant_pending"
    MERCHANT_CASH = "merchant_cash"
    PROVIDER_FEES_EXPENSE = "provider_fees_expense"

    @property
    def normal_side(self) -> Side:
        """The side on which the account's balance is reported as positive.

        The chart follows the lesson's directions: settled funds and the fee are booked as credit legs, so
        `merchant_cash` and `provider_fees_expense` read positive on the credit side. The invariant (every
        entry sums to zero) does not depend on this choice.
        """
        return Side.DEBIT if self is Account.CUSTOMER_RECEIVABLE else Side.CREDIT
