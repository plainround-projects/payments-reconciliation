"""The provider's fee schedule as the simulation models it: basis points of the amount, rounded half up."""

from __future__ import annotations

from dataclasses import dataclass

from payments_reconciliation.domain.errors import InvalidMoneyError
from payments_reconciliation.domain.money import Money


@dataclass(frozen=True, slots=True)
class FeePolicy:
    """A fee of `basis_points` hundredths of a percent of the amount, rounded half up to a minor unit."""

    basis_points: int

    def __post_init__(self) -> None:
        if not 0 <= self.basis_points <= 10_000:
            raise InvalidMoneyError("basis points must be between 0 and 10000")

    def fee_for(self, amount: Money) -> Money:
        """The fee on `amount`: 4250 cents at 290 basis points is 123 cents."""
        if not amount.is_positive():
            raise InvalidMoneyError("a fee is charged on a positive amount")
        return Money((amount.minor * self.basis_points + 5_000) // 10_000, amount.currency)
