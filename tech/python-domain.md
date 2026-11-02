# Python walkthrough: the domain package, line by line

## What it is

This note reads every line of the `domain` package of this repository: the rules of money, the double-entry ledger, the payment as the ledger holds it, the settlement report and its CSV file, and the matching and classification of differences. It is the line-by-line companion of `python.md`; read that note first for the language itself. Each block below is a piece of a real file, copied exactly, followed by an explanation in plain words.

A *package* is a folder of Python files that other code can import. The `domain` package is the heart of the service: the part that knows what money is, what a settlement row is and why two sides can disagree. It uses only Python's standard library.

## Why this repository uses it

The lesson's hardest promise is that every difference between the ledger and the provider's report is found, labeled with its cause and handled by the right rule. Those decisions are made here, as pure functions. Keeping them in a package that imports no web, database or framework code means they can be tested in microseconds with plain values, and a rule can never depend on how the data happens to be stored.

## Where it appears

| File | What it is |
| --- | --- |
| `src/payments_reconciliation/__init__.py` | The top-level package. |
| `src/payments_reconciliation/domain/__init__.py` | The domain package. |
| `src/payments_reconciliation/domain/errors.py` | The named errors. |
| `src/payments_reconciliation/domain/accounts.py` | The chart of accounts and sides. |
| `src/payments_reconciliation/domain/money.py` | Money as integer minor units. |
| `src/payments_reconciliation/domain/fees.py` | The provider's fee in basis points. |
| `src/payments_reconciliation/domain/ledger.py` | Balanced journal entries, reversals, refunds and fee corrections. |
| `src/payments_reconciliation/domain/payment.py` | A captured payment as the ledger holds it. |
| `src/payments_reconciliation/domain/settlement.py` | The settlement report, its rows and its CSV format. |
| `src/payments_reconciliation/domain/reconciliation.py` | Matching and classifying differences. |

## How it works underneath

When Python loads a module it runs the file from top to bottom once. `import` lines load other modules; `class` and `def` lines create classes and functions; nothing inside a function body runs until the function is called. A **class** is a blueprint; an **object** (instance) is one thing built from it. A **method** is a function defined inside a class and called on an object. A **type hint** (`x: int`, `-> str`) is a note for the programmer and for the mypy checker; Python itself ignores it when running. Money here is an integer number of *minor units* (cents), so all the arithmetic is exact. Matching is a **join**: both sides are grouped by the provider's transaction id, and for each id the code looks at how many entries each side has and how they compare.

## Walkthrough of this repository's use



### `src/payments_reconciliation/__init__.py` (1 lines)

```python
"""Daily reconciliation of a ledger against a provider's settlement report."""
```

This file is the whole top-level package. A line made only of text between three double quotes at the very top of a Python file is a **docstring**: a description that Python keeps with the module. Its presence turns the folder `payments_reconciliation/` into a **package**, a folder that other code can `import` from. The text says what the service does: every day it compares the ledger (our own book of money movements) with the **settlement report** (the file the payment provider sends about the money it actually settled) and explains each difference.

### `src/payments_reconciliation/domain/__init__.py` (1 lines)

```python
"""Money, the ledger, settlement rows, and the matching and classification rules. Standard library only."""
```

The docstring of the `domain` package. The domain is the part of the program that holds the rules and nothing else: money, the ledger, settlement rows, and the matching and classification rules. "Standard library only" is a promise that an automatic check (import-linter, see `import-linter.md`) enforces: no web framework, no database driver and no other outside library may be imported here.

### `src/payments_reconciliation/domain/errors.py` (55 lines)

```python
"""Named errors of the domain. Each carries a stable `code` that clients and logs can rely on."""

from __future__ import annotations
```

The module docstring says what the file is for: every error the service raises on purpose has a name, and each name carries a short stable text, its `code`, so that clients and log readers can rely on it.

`from __future__ import annotations` is a line that appears at the top of almost every module here. It tells Python to keep type hints (the `: str` and `-> None` notes) as text instead of evaluating them when the file loads. That lets a hint refer to a class that is defined later in the file and avoids circular imports.

```python
class ReconciliationError(Exception):
    """Base of every error this service raises on purpose."""

    code = "reconciliation_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
```

A **class** is a blueprint for objects. `class ReconciliationError(Exception)` makes a new kind of error by inheriting from Python's built-in `Exception`: it behaves like any error that can be raised with `raise` and caught with `except`, plus whatever is added here.

`code = "reconciliation_error"` is a **class attribute**: one value shared by all objects of the class. Subclasses replace it with their own text.

`def __init__(self, message: str) -> None:` is the **constructor**, the method that runs when an error object is created. `self` is the object being built. `super().__init__(message)` runs the constructor of the parent class `Exception`, so the standard behavior (printing the message in a traceback) still works. `self.message = message` also keeps the text on the object as an attribute, so the interfaces can read `error.message` without converting the error to text.

```python
class InvalidMoneyError(ReconciliationError):
    """An amount or currency that cannot be represented."""

    code = "invalid_money"
```

An amount or currency that cannot be represented (a text instead of a number, an unknown currency, too many decimals). A class body with only a docstring and a `code` is enough: it inherits the constructor.

```python
class CurrencyMismatchError(ReconciliationError):
    """Two amounts in different currencies were combined."""

    code = "currency_mismatch"
```

Raised when two amounts in different currencies are added, compared or subtracted.

```python
class UnbalancedEntryError(ReconciliationError):
    """A journal entry that does not have one debit and one equal credit on different accounts."""

    code = "unbalanced_entry"
```

Raised when someone tries to build a ledger entry that does not consist of exactly one debit and one equal credit on two different accounts. The domain refuses to even create such an entry.

```python
class SettlementFormatError(ReconciliationError):
    """A settlement file or row that does not have the provider's shape."""

    code = "settlement_format"
```

Raised when a settlement file or one of its rows does not have the provider's shape (wrong header, wrong column count, bad number, bad time). The message names the line.

```python
class SettlementMissingError(ReconciliationError):
    """No settlement report exists for the business day."""

    code = "settlement_missing"
```

Raised when no settlement report exists for the business day being reconciled.

```python
class PaymentNotFoundError(ReconciliationError):
    """No payment has this provider transaction id."""

    code = "payment_not_found"
```

Raised when a provider transaction id is looked up and no payment in the ledger carries it.

```python
class InvalidDateError(ReconciliationError):
    """The business date is not an ISO date (YYYY-MM-DD)."""

    code = "invalid_date"
```

Raised when a business date is not written as an ISO date (`YYYY-MM-DD`).

### `src/payments_reconciliation/domain/accounts.py` (31 lines)

```python
"""The chart of accounts. Sign convention of every posting: positive is a debit, negative is a credit."""

from __future__ import annotations

from enum import StrEnum
```

The docstring states the sign convention used by every ledger row: a **positive** amount is a **debit**, a **negative** amount is a **credit**. A *debit* and a *credit* are the two sides of a double-entry book; every movement of money is written once as each, and they always sum to zero.

`from enum import StrEnum` imports a helper for **enums**. An enum is a fixed set of named values.

```python
class Side(StrEnum):
    """Which side increases an account."""

    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
```

`Side` has two members, `DEBIT` and `CREDIT`. Because it inherits from `StrEnum` (Python 3.11 and newer), each member is also an ordinary string equal to its value, which makes it easy to store in the database and to send in JSON.

```python
class Account(StrEnum):
    """The four accounts of the lesson. The value is the account code stored in the database."""

    CUSTOMER_RECEIVABLE = "customer_receivable"
    MERCHANT_PENDING = "merchant_pending"
    MERCHANT_CASH = "merchant_cash"
    PROVIDER_FEES_EXPENSE = "provider_fees_expense"
```

The chart of accounts: the four places money can be recorded. `customer_receivable` is money the cardholder owes for captured payments; `merchant_pending` is a clearing account between capture and payout; `merchant_cash` is the merchant's money after the provider's fee; `provider_fees_expense` is the fee the provider keeps. The text of each value is exactly the account code stored in the database table `accounts`.

```python
    @property
    def normal_side(self) -> Side:
        """The side on which the account's balance is reported as positive.

        The chart follows the lesson's directions: settled funds and the fee are booked as credit legs, so
        `merchant_cash` and `provider_fees_expense` read positive on the credit side. The invariant (every
        entry sums to zero) does not depend on this choice.
        """
        return Side.DEBIT if self is Account.CUSTOMER_RECEIVABLE else Side.CREDIT
```

`@property` is a **decorator**: a line that wraps the function under it. It makes `normal_side` readable like an attribute (`account.normal_side`, no parentheses). The method returns the side on which the account's balance is reported as positive: `Side.DEBIT if self is Account.CUSTOMER_RECEIVABLE else Side.CREDIT`. `is` checks that two names refer to the very same object, which is the right test for enum members. The docstring explains the convention: settled funds and the fee are booked as credit legs, so those two accounts read positive on the credit side. Whichever convention is chosen, every entry still sums to zero.

### `src/payments_reconciliation/domain/money.py` (108 lines)

```python
"""Money as an integer count of minor units (cents) with a currency. Never a float."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from payments_reconciliation.domain.errors import CurrencyMismatchError, InvalidMoneyError

# Minor-unit exponent per ISO 4217 currency this service accepts.
```

The docstring states the rule of this file: money is an **integer count of minor units** (cents for dollars) together with a currency, and never a floating-point number, because a float cannot store 0.10 exactly.

The imports: `dataclass` (explained below), `Decimal` and `InvalidOperation` from the standard library's `decimal` module (a number type that stores decimal digits exactly), and the two money errors from `errors.py`.

```python
CURRENCY_EXPONENTS: dict[str, int] = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}
```

A **dictionary** (`dict`) maps keys to values. This one maps each supported currency code to its **exponent**: how many decimal places the currency has (2 for dollars, euros and pounds, 0 for yen). The hint `dict[str, int]` says the keys are strings and the values integers. The comment above it names the standard (ISO 4217) these codes come from.

```python
@dataclass(frozen=True, slots=True)
class Money:
    """An amount in minor units. May be negative (a credit leg); a payment amount is checked positive elsewhere."""

    minor: int
    currency: str
```

`Money` is an immutable value made of two fields: `minor` (the integer amount in minor units, so 4250 means 42.50) and `currency` (the code such as `"USD"`). The docstring notes the amount may be negative because ledger credits are negative; whether a payment amount is positive is checked elsewhere.

```python
    def __post_init__(self) -> None:
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise InvalidMoneyError(f"minor units must be an integer, got {type(self.minor).__name__}")
        if self.currency not in CURRENCY_EXPONENTS:
            raise InvalidMoneyError(f"unsupported currency {self.currency!r}; supported: {sorted(CURRENCY_EXPONENTS)}")
```

The validation that runs on every construction. `isinstance(self.minor, bool)` is checked first because `bool` is a subclass of `int` in Python (`True` equals 1) and a payment amount of `True` would otherwise pass. Then `not isinstance(self.minor, int)` rejects text, floats and anything else. An unsupported currency is rejected with a message that lists the supported ones (`sorted(CURRENCY_EXPONENTS)`). The `f"..."` text is an **f-string**: `{...}` parts are replaced by values, and `!r` would show a value with quotes. Because this check runs in the constructor, no invalid `Money` can exist anywhere in the program.

```python
    @classmethod
    def zero(cls, currency: str) -> Money:
        """Zero in `currency`."""
        return cls(0, currency)
```

`@classmethod` marks a method that belongs to the class, not to one object; its first parameter is `cls` (the class itself). `zero` is a handy constructor for an amount of zero: `cls(0, currency)`.

```python
    @classmethod
    def parse(cls, text: str, currency: str) -> Money:
        """Read a decimal string such as `42.50`; more fractional digits than the currency has is an error."""
        if currency not in CURRENCY_EXPONENTS:
            raise InvalidMoneyError(f"unsupported currency {currency!r}")
        exponent = CURRENCY_EXPONENTS[currency]
        try:
            value = Decimal(text)
        except InvalidOperation as error:
            raise InvalidMoneyError(f"not an amount: {text!r}") from error
        if not value.is_finite():
            raise InvalidMoneyError(f"not an amount: {text!r}")
        scaled = value.scaleb(exponent)
        if scaled != scaled.to_integral_value():
            raise InvalidMoneyError(f"{text!r} has more than {exponent} decimal places for {currency}")
        return cls(int(scaled), currency)
```

`parse` reads a decimal text such as `"42.50"` and returns `Money(4250, "USD")`, refusing anything that does not fit. Step by step: an unknown currency is refused. `exponent` is looked up. `Decimal(text)` converts the text exactly; if the text is not a number, `Decimal` raises `InvalidOperation`, which the `try`/`except` catches and re-raises as `InvalidMoneyError` using `raise ... from error` (that keeps the original error attached as the cause). `value.is_finite()` rejects `NaN` and infinity, which `Decimal` accepts. `value.scaleb(exponent)` moves the decimal point right by `exponent` places, so `Decimal("42.50").scaleb(2)` is `4250`. If the result still has a fractional part (`scaled != scaled.to_integral_value()`, for example "42.505" for a two-decimal currency) the text had too many decimals and is refused. Otherwise `int(scaled)` is the integer amount.

```python
    @property
    def exponent(self) -> int:
        """Decimal places of the currency."""
        return CURRENCY_EXPONENTS[self.currency]
```

`exponent` is a read-only property that returns the number of decimal places of this amount's currency, using the dictionary above.

```python
    def is_positive(self) -> bool:
        """True when the amount is greater than zero."""
        return self.minor > 0

    def is_zero(self) -> bool:
        """True when the amount is zero."""
        return self.minor == 0
```

Two small questions about an amount: is it greater than zero, and is it exactly zero. They return a `bool` (`True` or `False`).

```python
    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._same_currency(other)
        return self.minor >= other.minor
```

**Operator methods.** Python calls `__add__` when you write `a + b`, `__sub__` for `a - b`, `__neg__` for `-a`, and `__lt__`, `__le__`, `__gt__`, `__ge__` for `<`, `<=`, `>`, `>=`. Defining them lets `Money` objects be used with ordinary arithmetic. Each two-operand method first calls `self._same_currency(other)`, which raises `CurrencyMismatchError` when the currencies differ, and then works on the integer `minor` values, building a new `Money` (the old ones never change).

```python
    def format(self) -> str:
        """Decimal text with the currency's own number of places, for example `42.50` or `-1.23`."""
        sign = "-" if self.minor < 0 else ""
        digits = str(abs(self.minor))
        if self.exponent == 0:
            return f"{sign}{digits}"
        digits = digits.rjust(self.exponent + 1, "0")
        return f"{sign}{digits[: -self.exponent]}.{digits[-self.exponent :]}"
```

`format` turns an amount into display text with the currency's own number of decimals: `Money(4250, "USD").format()` is `"42.50"`, `Money(-123, "USD").format()` is `"-1.23"` and yen show no decimal point. How: `sign` is `"-"` for negatives; `digits = str(abs(self.minor))` is the digits without sign; `rjust(self.exponent + 1, "0")` pads on the left with zeros so there is always at least one digit before the decimal point (5 cents becomes `"005"`); then the string is cut with **slices**: `digits[: -self.exponent]` is everything except the last `exponent` characters (the whole part) and `digits[-self.exponent :]` is the last `exponent` characters (the decimals), joined with a dot. For a currency with no decimals the digits are returned as they are.

```python
    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot combine {self.currency} with {other.currency}")
```

A private helper (the leading underscore is the convention for "internal"). It raises `CurrencyMismatchError` with both currency codes in the message when they differ.

```python
def sum_money(amounts: list[Money] | tuple[Money, ...], currency: str) -> Money:
    """Add `amounts`; the empty sum is zero in `currency`."""
    total = Money.zero(currency)
    for amount in amounts:
        total = total + amount
    return total
```

A plain function that adds a list or tuple of `Money` values. The type `list[Money] | tuple[Money, ...]` means "a list of Money, or a tuple of any length of Money". The empty sum is `Money.zero(currency)`; that is why the function needs the currency as an argument. The `for` loop adds each amount to a running `total` using the `+` defined above, so mixing currencies raises the mismatch error here too.

### `src/payments_reconciliation/domain/fees.py` (25 lines)

```python
"""The provider's fee schedule as the simulation models it: basis points of the amount, rounded half up."""

from __future__ import annotations

from dataclasses import dataclass

from payments_reconciliation.domain.errors import InvalidMoneyError
from payments_reconciliation.domain.money import Money
```

The module docstring: the provider's fee, modeled as a percentage written in **basis points** (one basis point is one hundredth of a percent, so 290 basis points is 2.90 percent) and **rounded half up**.

`from dataclasses import dataclass` imports the tool that writes the boring parts of a class (see below). The two imports from the repository bring in `InvalidMoneyError` and `Money`.

```python
@dataclass(frozen=True, slots=True)
class FeePolicy:
    """A fee of `basis_points` hundredths of a percent of the amount, rounded half up to a minor unit."""

    basis_points: int
```

A **dataclass** generates `__init__`, `__repr__` and `__eq__` for a class from its listed fields. `frozen=True` makes instances immutable (assigning to a field raises an error). `slots=True` stores the fields in a fixed layout, saving memory and rejecting misspelled attribute names. The single field is `basis_points: int`.

```python
    def __post_init__(self) -> None:
        if not 0 <= self.basis_points <= 10_000:
            raise InvalidMoneyError("basis points must be between 0 and 10000")
```

`__post_init__` runs right after the generated constructor and **validates**. `0 <= self.basis_points <= 10_000` is a chained comparison (true when the number is between 0 and 10,000, the underscore is just a readable thousands separator). Anything else raises `InvalidMoneyError`, so a `FeePolicy` with a negative or absurd rate cannot exist.

```python
    def fee_for(self, amount: Money) -> Money:
        """The fee on `amount`: 4250 cents at 290 basis points is 123 cents."""
        if not amount.is_positive():
            raise InvalidMoneyError("a fee is charged on a positive amount")
        return Money((amount.minor * self.basis_points + 5_000) // 10_000, amount.currency)
```

`fee_for` computes the fee on an amount. A fee is only charged on a positive amount. The formula `(amount.minor * self.basis_points + 5_000) // 10_000` works in whole numbers only: multiply the cents by the basis points, add 5,000 (half of 10,000) and divide with `//`, which is **integer division** (it drops the remainder). Adding half the divisor before dividing is how "round half up" is done without floating-point numbers. Example from the docstring: 4250 cents at 290 basis points is (4250 x 290 + 5000) // 10000 = 123 cents. The result is wrapped in a new `Money` with the same currency.

### `src/payments_reconciliation/domain/ledger.py` (128 lines)

```python
"""The double-entry ledger: entries that cannot be unbalanced, and the entries of each operation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from payments_reconciliation.domain.accounts import Account, Side
from payments_reconciliation.domain.errors import UnbalancedEntryError
from payments_reconciliation.domain.money import Money
```

The docstring: entries that cannot be unbalanced, and the entries of each operation (a capture, a reversal, a refund, a fee correction). The imports include `Iterable` (anything you can loop over), `StrEnum`, and the `Account`, `Side`, `Money` and `UnbalancedEntryError` names from the sibling files.

```python
class Leg(StrEnum):
    """Which part of an operation an entry books. The pair (payment, operation, reference, leg) is unique."""

    CAPTURE_GROSS = "CAPTURE_GROSS"
    CAPTURE_NET = "CAPTURE_NET"
    CAPTURE_FEE = "CAPTURE_FEE"
    REFUND_GROSS = "REFUND_GROSS"
    REFUND_NET = "REFUND_NET"
    FEE_CORRECTION = "FEE_CORRECTION"
    REVERSAL_GROSS = "REVERSAL_GROSS"
    REVERSAL_NET = "REVERSAL_NET"
    REVERSAL_FEE = "REVERSAL_FEE"
```

A **leg** is one part of an operation. Naming every leg lets the database insist that each leg of an operation is booked at most once (the unique key on payment, operation, reference and leg). A capture has three legs (gross, net, fee) and a refund two. The reconciliation adds `FEE_CORRECTION` (moves a recorded fee to the provider's actual fee) and three reversal legs, one for each capture leg, which undo a capture that was booked twice. A `StrEnum` member is also a text, so `Leg.CAPTURE_GROSS == "CAPTURE_GROSS"`.

```python
_REVERSAL_OF = {
    Leg.CAPTURE_GROSS: Leg.REVERSAL_GROSS,
    Leg.CAPTURE_NET: Leg.REVERSAL_NET,
    Leg.CAPTURE_FEE: Leg.REVERSAL_FEE,
}
```

A lookup table (a dictionary) from each capture leg to the leg that undoes it. A dictionary maps a key to a value: `_REVERSAL_OF[Leg.CAPTURE_FEE]` is `Leg.REVERSAL_FEE`. The leading underscore says the name is private to this file.

```python
@dataclass(frozen=True, slots=True)
class Posting:
    """One row of the ledger: an account and a signed amount (positive debit, negative credit)."""

    account: Account
    amount: Money
```

`Posting` is one row of the ledger: an `Account` and a signed `Money`. Positive means debit, negative means credit.

```python
@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A debit and an equal credit on two different accounts. Any other shape cannot be built."""

    leg: Leg
    debit: Account
    credit: Account
    amount: Money

    def __post_init__(self) -> None:
        if not self.amount.is_positive():
            raise UnbalancedEntryError(f"an entry moves a positive amount, got {self.amount.format()}")
        if self.debit is self.credit:
            raise UnbalancedEntryError("the debit and the credit must be on different accounts")

    def postings(self) -> tuple[Posting, Posting]:
        """The two rows: +amount on the debit account, -amount on the credit account."""
        return Posting(self.debit, self.amount), Posting(self.credit, -self.amount)
```

`JournalEntry` is a debit and an equal credit on two different accounts, with a `leg` label. `__post_init__` makes the invalid shapes impossible: the amount must be positive, and `self.debit is self.credit` (the same account on both sides) is refused. After construction the entry always balances. `postings()` returns the two rows as a tuple: `+amount` on the debit account and `-amount` on the credit account (`-self.amount` uses the `__neg__` operator of `Money`). The return type `tuple[Posting, Posting]` says "exactly two postings".

```python
    def reversed(self) -> JournalEntry:
        """The same amount moved back: debit and credit swapped, on the matching reversal leg."""
        return JournalEntry(_REVERSAL_OF[self.leg], self.credit, self.debit, self.amount)
```

`reversed()` returns the same amount moved back: the debit and credit accounts swapped, labeled with the matching reversal leg from `_REVERSAL_OF`. Nothing is edited or deleted; the ledger keeps both the capture and its reversal.

```python
def capture_entries(amount: Money, fee: Money) -> tuple[JournalEntry, ...]:
    """The entries of a capture: gross to pending, net to cash, fee to the fee account (six rows).

    A leg that would move nothing is left out, so a fee that rounds to zero gives four rows.
    """
    if fee.minor < 0 or fee > amount:
        raise UnbalancedEntryError("the fee must be between zero and the captured amount")
    net = amount - fee
    entries = [JournalEntry(Leg.CAPTURE_GROSS, Account.CUSTOMER_RECEIVABLE, Account.MERCHANT_PENDING, amount)]
    if net.is_positive():
        entries.append(JournalEntry(Leg.CAPTURE_NET, Account.MERCHANT_PENDING, Account.MERCHANT_CASH, net))
    if fee.is_positive():
        entries.append(JournalEntry(Leg.CAPTURE_FEE, Account.MERCHANT_PENDING, Account.PROVIDER_FEES_EXPENSE, fee))
    return tuple(entries)
```

`capture_entries` builds the entries of a capture. The fee must be between zero and the amount. `net = amount - fee`. The entries: the gross amount moves from `CUSTOMER_RECEIVABLE` to `MERCHANT_PENDING`; the net amount moves from `MERCHANT_PENDING` to `MERCHANT_CASH` (only if it is positive); the fee moves from `MERCHANT_PENDING` to `PROVIDER_FEES_EXPENSE` (only if it is positive). A leg that would move nothing is left out, so a fee that rounds to 0 on a tiny amount gives four rows instead of six (a zero-amount entry would be refused). The result is a tuple, an unchangeable list. The worked example: 42.50 with a 1.23 fee gives rows of 4250, 4127 and 123.

```python
def reversal_entries(amount: Money, fee: Money) -> tuple[JournalEntry, ...]:
    """The entries that undo a capture of `amount` with `fee`, leg by leg. Nothing is deleted; the ledger shows both."""
    return tuple(entry.reversed() for entry in capture_entries(amount, fee))
```

`reversal_entries` undoes a capture leg by leg: it builds the capture entries and reverses each one with a **generator expression** (`tuple(entry.reversed() for entry in ...)`, which makes the tuple without building a list first). A reversal of a 42.50 capture with a 1.23 fee therefore has the same six rows with the signs flipped.

```python
def refund_entries(amount: Money) -> tuple[JournalEntry, ...]:
    """The two entries (four rows) of a refund: the capture's first two legs reversed. The fee is not returned."""
    return (
        JournalEntry(Leg.REFUND_GROSS, Account.MERCHANT_PENDING, Account.CUSTOMER_RECEIVABLE, amount),
        JournalEntry(Leg.REFUND_NET, Account.MERCHANT_CASH, Account.MERCHANT_PENDING, amount),
    )
```

A refund reverses the first two legs of a capture: the gross amount goes back from `MERCHANT_PENDING` to `CUSTOMER_RECEIVABLE`, and the refunded money leaves `MERCHANT_CASH` for `MERCHANT_PENDING`. The fee is not returned. Four rows in two entries.

```python
def fee_correction_entry(recorded: Money, actual: Money) -> JournalEntry | None:
    """The one entry that moves a recorded fee to the provider's actual fee, or None when they already agree.

    A higher actual fee takes the difference out of the merchant's cash and into the fee account; a lower one
    puts it back.
    """
    delta = actual - recorded
    if delta.is_zero():
        return None
    if delta.is_positive():
        return JournalEntry(Leg.FEE_CORRECTION, Account.MERCHANT_CASH, Account.PROVIDER_FEES_EXPENSE, delta)
    return JournalEntry(Leg.FEE_CORRECTION, Account.PROVIDER_FEES_EXPENSE, Account.MERCHANT_CASH, -delta)
```

When the provider charged a different fee than the one recorded, one entry moves the difference. `delta = actual - recorded`. If it is zero nothing is needed (`None`, meaning "no entry"; the return type `JournalEntry | None` says so). If the actual fee is higher (`delta` positive) the difference leaves the merchant's cash and goes to the fee expense account. If lower, the negated difference (`-delta`, positive) goes back from the fee account to cash. The entry is always for a positive amount, as `JournalEntry` requires.

```python
def entries_postings(entries: Iterable[JournalEntry]) -> list[Posting]:
    """Every row of `entries`, in order."""
    return [posting for entry in entries for posting in entry.postings()]
```

Flattens entries into their rows with a **list comprehension** with two `for` parts: for every entry, for every posting in that entry's `postings()`, collect the posting.

```python
def ledger_sum(postings: Iterable[Posting], currency: str) -> Money:
    """The sum of every posting. It is zero for any set of whole entries."""
    total = Money.zero(currency)
    for posting in postings:
        total = total + posting.amount
    return total
```

Adds all the rows into one `Money`. For any set of whole entries the result is zero, which is the ledger invariant.

```python
def balances(postings: Iterable[Posting], currency: str) -> dict[Account, Money]:
    """Each account's balance on its normal side (a credit-normal account reads positive after credits)."""
    raw = {account: Money.zero(currency) for account in Account}
    for posting in postings:
        raw[posting.account] = raw[posting.account] + posting.amount
    return {account: (total if account.normal_side is Side.DEBIT else -total) for account, total in raw.items()}
```

Computes each account's balance. `raw` starts at zero for every account (`{account: Money.zero(currency) for account in Account}` is a **dict comprehension**). Each posting's amount is added to its account. The last line reports each balance on its account's normal side: debit-normal accounts as they are, credit-normal accounts negated (`-total`), so that a credit-normal account reads positive after credits.

### `src/payments_reconciliation/domain/payment.py` (61 lines)

```python
"""A captured payment as the ledger knows it, carrying the provider's transaction id."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payments_reconciliation.domain.errors import ReconciliationError
from payments_reconciliation.domain.money import Money
```

The docstring: a captured payment as the ledger knows it, carrying the provider's transaction id. The imports: `dataclasses` (used for `dataclasses.replace`, which copies a frozen object with some fields changed), `dataclass`, `datetime`, `StrEnum` and `UUID`, plus `ReconciliationError` and `Money` from this project.

```python
class PaymentState(StrEnum):
    """CAPTURED payments count in reconciliation; a duplicate that was reversed does not."""

    CAPTURED = "CAPTURED"
    DUPLICATE_REVERSED = "DUPLICATE_REVERSED"
```

The two states a payment can have here. `CAPTURED` payments count in reconciliation. `DUPLICATE_REVERSED` is a duplicate that was booked twice and then reversed; it no longer counts as money moved.

```python
class PaymentSource(StrEnum):
    """How the payment got into the ledger."""

    SERVICE = "SERVICE"  # the capture path, as it should work
    SERVICE_RETRY = "SERVICE_RETRY"  # recorded a second time by a retry (the duplicate break)
    RECONCILIATION = "RECONCILIATION"  # inserted from the settlement report (the missing-capture break)
```

How a payment got into the ledger. `SERVICE` is the normal capture path. `SERVICE_RETRY` is a payment recorded a second time by a retry (this is how a duplicate on our side arises). `RECONCILIATION` is a payment inserted from the settlement report because the ledger had missed it. The text after `#` on each line is a comment.

```python
@dataclass(frozen=True, slots=True)
class LedgerPayment:
    """One row of `payments`. Several rows may carry one provider transaction id; that is the duplicate break."""

    id: UUID
    provider_txn_id: str
    amount: Money
    fee: Money
    state: PaymentState
    source: PaymentSource
    captured_at: datetime
    refunded: Money
```

`LedgerPayment` is one row of the `payments` table as a Python object: its id (a `UUID`), the provider's transaction id (the join key to the report), the amount, the fee, the state, the source, when it was captured and how much has been refunded. `frozen=True` makes it unchangeable after creation (a change is a new object); `slots=True` stores the fields compactly. The docstring notes that several rows may carry one provider transaction id: that is the duplicate break.

```python
    @property
    def active(self) -> bool:
        """True while the payment counts as money moved."""
        return self.state is PaymentState.CAPTURED
```

The property `active` (a method used without parentheses, thanks to the `@property` line above it) is true while the payment counts as money moved, that is, while its state is `CAPTURED`. `is` compares identity, which is the right test for enum members.

```python
    def with_fee(self, fee: Money) -> LedgerPayment:
        """The same payment with the provider's actual fee."""
        return dataclasses.replace(self, fee=fee)
```

`dataclasses.replace(self, fee=fee)` returns a copy of the payment with only the fee changed. This is how a fee correction is applied to the in-memory object.

```python
    def reversed_as_duplicate(self) -> LedgerPayment:
        """The same payment, no longer counted."""
        return dataclasses.replace(self, state=PaymentState.DUPLICATE_REVERSED)
```

The same payment with the state `DUPLICATE_REVERSED`.

```python
    def with_refund(self, amount: Money) -> LedgerPayment:
        """The same payment after refunding `amount` more; never more than was captured."""
        total = self.refunded + amount
        if not amount.is_positive() or total > self.amount:
            raise ReconciliationError(f"refund of {amount.format()} does not fit a capture of {self.amount.format()}")
        return dataclasses.replace(self, refunded=total)
```

Adds a refund. `total` is the refunded amount so far plus the new one. The refund must be positive and the total must not exceed the captured amount; otherwise a `ReconciliationError` explains it. Otherwise a copy with the new refunded total is returned.

### `src/payments_reconciliation/domain/settlement.py` (118 lines)

```python
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
```

The provider's settlement report: its rows, and reading and writing the CSV file. New imports: `csv` (reads and writes comma-separated text), `io` (in-memory text streams), `re` (regular expressions), and `date`, `datetime` and `UTC` for times.

```python
HEADER = ["provider_txn_id", "amount", "fee", "status", "settled_at"]
_TXN_ID = re.compile(r"^ptx_[A-Za-z0-9]{1,32}$")
```

`HEADER` is the list of the five column names every settlement file must start with. `_TXN_ID` is a **regular expression**, a pattern for text: `^ptx_[A-Za-z0-9]{1,32}$` means the whole text (`^` start, `$` end) is `ptx_` followed by 1 to 32 letters or digits.

```python
class SettlementKind(StrEnum):
    """`settled` rows move money to the merchant; `refunded` rows take it back (negative amount)."""

    SETTLED = "settled"
    REFUNDED = "refunded"
```

The two kinds of row. `settled` rows move money to the merchant; `refunded` rows take it back and carry a negative amount.

```python
@dataclass(frozen=True, slots=True)
class SettlementRow:
    """One line of the report. The provider's transaction id is the join key; a refund shares its capture's id."""

    provider_txn_id: str
    kind: SettlementKind
    amount: Money
    fee: Money
    settled_at: datetime
```

`SettlementRow` is one line of the report: the provider's transaction id (the join key; a refund shares its capture's id), the kind, the amount, the fee and the time the provider settled it.

```python
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
```

`__post_init__` runs right after the object is built and refuses bad rows with `SettlementFormatError`: an id that does not match `_TXN_ID`; a time without a timezone (`tzinfo is None`); a settled row whose amount is not positive; a refunded row whose amount is not negative; a negative fee. `{self.provider_txn_id!r}` prints the id with quotes (`!r` means "show the programmer's form").

```python
@dataclass(frozen=True, slots=True)
class SettlementReport:
    """All rows the provider settled for one business day."""

    business_date: date
    currency: str
    rows: tuple[SettlementRow, ...]

    def of_kind(self, kind: SettlementKind) -> tuple[SettlementRow, ...]:
        """The rows of one kind, in file order."""
        return tuple(row for row in self.rows if row.kind is kind)
```

`SettlementReport` is all the rows of one business day with the day and the currency. `rows` is a tuple (an unchangeable list). `of_kind` returns only the rows of one kind, in file order, with a generator expression.

```python
    @property
    def settled_total(self) -> Money:
        """The sum of the `settled` rows."""
        total = Money.zero(self.currency)
        for row in self.of_kind(SettlementKind.SETTLED):
            total = total + row.amount
        return total
```

`settled_total` adds up the amounts of the settled rows, starting from zero in the report's currency.

```python
    @property
    def refunded_total(self) -> Money:
        """The sum of the `refunded` rows (zero or negative)."""
        total = Money.zero(self.currency)
        for row in self.of_kind(SettlementKind.REFUNDED):
            total = total + row.amount
        return total
```

`refunded_total` does the same for the refunded rows; the result is zero or negative.

```python
def format_csv(rows: Iterable[SettlementRow]) -> str:
    """The provider's file: `provider_txn_id,amount,fee,status,settled_at`, amounts in decimal text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for row in rows:
        stamp = row.settled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        writer.writerow([row.provider_txn_id, row.amount.format(), row.fee.format(), row.kind.value, stamp])
    return buffer.getvalue()
```

`format_csv` writes the provider's file. A `StringIO` is a text buffer in memory. `csv.writer` writes comma-separated lines; `lineterminator="\n"` fixes the line ending. It writes the header, then for each row the id, the amount and the fee as decimal text (`format()`), the kind and the time converted to UTC and written like `2026-10-20T09:15:00Z` (`strftime` formats a time). `getvalue()` returns everything written.

```python
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
```

`parse_csv` reads the file back and is strict: nothing is skipped silently. `csv.reader` splits lines into lists of fields. `next(reader)` takes the first line; if there is none (`StopIteration`) the file is empty and `SettlementFormatError` is raised (`from error` keeps the original error as the cause). The header must equal `HEADER`. Then `enumerate(reader, start=2)` numbers the data lines from 2 (line 1 is the header). An empty record is skipped; a record with the wrong number of columns names its line number. The five fields are unpacked into names, and a `SettlementRow` is built: the status text becomes a `SettlementKind` (an unknown status raises `ValueError`), the amounts become `Money` via `Money.parse`, and the time is read with `strptime` and given the UTC zone. Any `ValueError` or `ReconciliationError` is re-raised as a `SettlementFormatError` that starts with `line N:`. At the end the report is returned with the rows as a tuple.

### `src/payments_reconciliation/domain/reconciliation.py` (251 lines)

```python
"""Matching the ledger against a settlement report and classifying every difference by its cause.

Everything here is a pure function of its inputs: no clock, no database, no files.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum

from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment
from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, SettlementRow
```

Matching the ledger against a settlement report and classifying every difference by its cause. The docstring promises that everything here is a **pure function** of its inputs: no clock, no database, no files, so the same inputs always give the same answer and tests need no setup. `defaultdict` is a dictionary that creates a missing entry (here an empty list) on first use; `Mapping` is the type of a read-only dictionary; `field` sets a default for a dataclass field; `time` is a time of day.

```python
class Cause(StrEnum):
    """Why a transaction does not match. One label per cause, never a shared "mismatch" bucket."""

    TIMING = "timing"
    FEE_DISCREPANCY = "fee_discrepancy"
    MISSING_CAPTURE = "missing_capture"
    DUPLICATE_ON_OUR_SIDE = "duplicate_on_our_side"
    PROVIDER_ONLY_REFUND = "provider_only_refund"
    AMOUNT_MISMATCH = "amount_mismatch"
    # The causes below are the ones no rule can clear safely; they go to a person.
    LEDGER_ONLY_UNEXPLAINED = "ledger_only_unexplained"
    TIMING_OVERDUE = "timing_overdue"
    PROVIDER_DUPLICATE = "provider_duplicate"
    REFUND_ONLY_IN_LEDGER = "refund_only_in_ledger"
    REFUND_AMOUNT_MISMATCH = "refund_amount_mismatch"
```

`Cause` is the list of reasons a transaction can fail to match. The docstring insists on one label per cause, never a shared "mismatch" bucket, because each cause has a different remedy. In order: `TIMING` (we captured it, the provider has not settled it yet); `FEE_DISCREPANCY` (both sides have it, the fees differ); `MISSING_CAPTURE` (settled at the provider, absent from our ledger); `DUPLICATE_ON_OUR_SIDE` (several ledger payments for one provider id); `PROVIDER_ONLY_REFUND` (the provider refunded, we recorded nothing); `AMOUNT_MISMATCH` (both have it, the amounts differ). The comment says the causes after it are the ones no rule can clear safely: `LEDGER_ONLY_UNEXPLAINED`, `TIMING_OVERDUE` (waited longer than allowed), `PROVIDER_DUPLICATE` (the report lists it twice), `REFUND_ONLY_IN_LEDGER` and `REFUND_AMOUNT_MISMATCH`.

```python
class Handling(StrEnum):
    """What is done about a cause."""

    CARRY = "carry"  # wait: the next day's report should contain it
    AUTOMATIC = "automatic"  # a balanced correcting entry from the report's own number
    FROM_REPORT = "from_report"  # fix the ledger from the provider's authoritative data
    HUMAN = "human"  # nothing here can decide
```

What is done about a cause: `CARRY` (wait; the next day's report should contain it), `AUTOMATIC` (a balanced correcting entry computed from the report's own number), `FROM_REPORT` (fix the ledger from the provider's authoritative data) or `HUMAN` (nothing here can decide, so a person must).

```python
HANDLING: Mapping[Cause, Handling] = {
    Cause.TIMING: Handling.CARRY,
    Cause.FEE_DISCREPANCY: Handling.AUTOMATIC,
    Cause.MISSING_CAPTURE: Handling.FROM_REPORT,
    Cause.DUPLICATE_ON_OUR_SIDE: Handling.FROM_REPORT,
    Cause.PROVIDER_ONLY_REFUND: Handling.FROM_REPORT,
    Cause.AMOUNT_MISMATCH: Handling.HUMAN,
    Cause.LEDGER_ONLY_UNEXPLAINED: Handling.HUMAN,
    Cause.TIMING_OVERDUE: Handling.HUMAN,
    Cause.PROVIDER_DUPLICATE: Handling.HUMAN,
    Cause.REFUND_ONLY_IN_LEDGER: Handling.HUMAN,
    Cause.REFUND_AMOUNT_MISMATCH: Handling.HUMAN,
}
```

The rulebook as a dictionary from each cause to its handling. Timing is carried; a fee discrepancy is corrected automatically; a missing capture, a duplicate on our side and a provider-only refund are fixed from the report; every other cause goes to a human. The type `Mapping[Cause, Handling]` says it is read-only.

```python
@dataclass(frozen=True, slots=True)
class Break:
    """One difference between the two sides, with the numbers that show it."""

    provider_txn_id: str
    kind: SettlementKind
    cause: Cause
    ledger_amount: Money | None
    settlement_amount: Money | None
    ledger_fee: Money | None
    settlement_fee: Money | None
    detail: str

    @property
    def handling(self) -> Handling:
        """What is done about this break's cause."""
        return HANDLING[self.cause]
```

`Break` is one difference between the two sides, with the numbers that show it: the provider transaction id, the kind of row, the cause, the amount and the fee on each side (`Money | None` means "a `Money` or nothing", for the side that lacks the transaction) and a human-readable `detail`. The property `handling` looks the cause up in `HANDLING`.

```python
@dataclass(frozen=True, slots=True)
class LedgerView:
    """Our side of one business day: the payments to compare and the refunds recorded that day, by provider id."""

    payments: tuple[LedgerPayment, ...]
    refunds: Mapping[str, Money] = field(default_factory=dict)
```

`LedgerView` is our side of one business day: the payments to compare, and the refunds recorded that day as a dictionary from provider id to amount (`field(default_factory=dict)` gives each object its own empty dictionary by default).

```python
@dataclass(frozen=True, slots=True)
class Matched:
    """A transaction both sides agree on."""

    provider_txn_id: str
    kind: SettlementKind
    amount: Money
```

A transaction both sides agree on: its id, kind and amount.

```python
@dataclass(frozen=True, slots=True)
class MatchResult:
    """Everything one pass found."""

    matched: tuple[Matched, ...]
    breaks: tuple[Break, ...]

    def break_ids(self) -> set[str]:
        """Provider ids that have at least one break."""
        return {item.provider_txn_id for item in self.breaks}
```

Everything one pass found: the matched items and the breaks (both tuples). `break_ids()` returns the set (`{...}` with no repeats) of provider ids that have at least one break.

```python
def cutoff_instant(business_date: date, cutoff: time) -> datetime:
    """The moment the provider closes the batch of `business_date`; captures at or after it settle the next day."""
    return datetime.combine(business_date, cutoff, tzinfo=UTC)
```

The provider closes its daily batch at a fixed time of day, the **cutoff**. `datetime.combine(business_date, cutoff, tzinfo=UTC)` joins the day and the time into one moment. A capture at or after that moment settles on the next day.

```python
def reconcile(
    ledger: LedgerView,
    report: SettlementReport,
    *,
    carried_days: Mapping[str, int],
    cutoff: time,
    max_carry_days: int,
) -> MatchResult:
    """Join the two sides on the provider's transaction id and classify what does not agree.

    `carried_days` maps a transaction id to how many days it has waited as a timing difference.
    """
```

The main function. The `*` in the parameter list means everything after it must be passed by name. `carried_days` says how many days each transaction has already waited as a timing difference; `cutoff` and `max_carry_days` are the rules.

```python
    currency = report.currency
    ours: dict[str, list[LedgerPayment]] = defaultdict(list)
    for payment in ledger.payments:
        if payment.active:
            ours[payment.provider_txn_id].append(payment)
    settled: dict[str, list[SettlementRow]] = defaultdict(list)
    refunded: dict[str, list[SettlementRow]] = defaultdict(list)
    for row in report.rows:
        (settled if row.kind is SettlementKind.SETTLED else refunded)[row.provider_txn_id].append(row)
```

The body. First it groups our side: `ours` is a `defaultdict(list)`, so `ours[id].append(payment)` works for an id seen for the first time. Only `active` payments are included (a reversed duplicate is ignored). Then the report's rows are grouped by id into `settled` and `refunded` with one line: `(settled if row.kind is SettlementKind.SETTLED else refunded)[row.provider_txn_id].append(row)` picks the right dictionary first and then appends to its list.

```python
    matched: list[Matched] = []
    breaks: list[Break] = []
    for txn_id in sorted(set(ours) | set(settled)):
        found = _classify_settled(
            txn_id,
            ours.get(txn_id, []),
            settled.get(txn_id, []),
            report=report,
            carried_days=carried_days.get(txn_id),
            cutoff=cutoff,
            max_carry_days=max_carry_days,
        )
        if isinstance(found, Break):
            breaks.append(found)
        elif found is not None:
            matched.append(found)
    for txn_id in sorted(set(ledger.refunds) | set(refunded)):
        found = _classify_refunds(txn_id, ledger.refunds.get(txn_id), refunded.get(txn_id, []), currency)
        if isinstance(found, Break):
            breaks.append(found)
        elif found is not None:
            matched.append(found)
    return MatchResult(tuple(matched), tuple(breaks))
```

Two result lists. The loop `for txn_id in sorted(set(ours) | set(settled))` visits every id seen on either side (`|` is the union of two sets) in alphabetical order so results are repeatable. `_classify_settled` returns a `Break`, a `Matched`, or `None`; `isinstance(found, Break)` tests which. The next loop does the same for refunds over the ids in either side's refunds. At the end the result is returned with both lists turned into tuples.

```python
def _classify_settled(
    txn_id: str,
    ours: list[LedgerPayment],
    theirs: list[SettlementRow],
    *,
    report: SettlementReport,
    carried_days: int | None,
    cutoff: time,
    max_carry_days: int,
) -> Break | Matched | None:
```

Decides what one settled transaction is, looking at how many payments we have and how many rows the report has. The leading underscore marks it private to the file. Its return type `Break | Matched | None` lists the three possible results.

```python
    kind = SettlementKind.SETTLED
    if len(theirs) > 1:
        total = sum((row.amount.minor for row in theirs), 0)
        return Break(
            txn_id, kind, Cause.PROVIDER_DUPLICATE, _total(ours), Money(total, report.currency), None, None,
            f"the report lists {len(theirs)} settled rows for one transaction",
        )  # fmt: skip
    if len(ours) > 1:
        return Break(
            txn_id, kind, Cause.DUPLICATE_ON_OUR_SIDE, ours[0].amount, theirs[0].amount if theirs else None,
            ours[0].fee, theirs[0].fee if theirs else None,
            f"{len(ours)} payments in the ledger carry this provider transaction id",
        )  # fmt: skip
    if not ours:
        row = theirs[0]
        return Break(
            txn_id, kind, Cause.MISSING_CAPTURE, None, row.amount, None, row.fee,
            "settled at the provider, no payment in the ledger",
        )  # fmt: skip
    payment = ours[0]
    if not theirs:
        return _ledger_only(txn_id, payment, report.business_date, carried_days, cutoff, max_carry_days)
    row = theirs[0]
    if payment.amount != row.amount:
        return Break(
            txn_id, kind, Cause.AMOUNT_MISMATCH, payment.amount, row.amount, payment.fee, row.fee,
            f"ledger {payment.amount.format()}, settlement {row.amount.format()}",
        )  # fmt: skip
    if payment.fee != row.fee:
        return Break(
            txn_id, kind, Cause.FEE_DISCREPANCY, payment.amount, row.amount, payment.fee, row.fee,
            f"fee recorded {payment.fee.format()}, settled {row.fee.format()}",
        )  # fmt: skip
    return Matched(txn_id, kind, payment.amount)
```

The order of the tests matters. (1) More than one report row for one id: `PROVIDER_DUPLICATE`; the total is summed with `sum(..., 0)`. (2) More than one payment of ours: `DUPLICATE_ON_OUR_SIDE`. (3) No payment of ours: `MISSING_CAPTURE`. `# fmt: skip` after a closing bracket tells the code formatter to leave that layout alone. (4) We have it but the report does not: `_ledger_only` decides between timing and trouble. (5) Both have it but the amounts differ: `AMOUNT_MISMATCH`. (6) Amounts agree but fees differ: `FEE_DISCREPANCY`. (7) Otherwise the two sides agree and a `Matched` is returned.

```python
def _ledger_only(
    txn_id: str,
    payment: LedgerPayment,
    business_date: date,
    carried_days: int | None,
    cutoff: time,
    max_carry_days: int,
) -> Break:
    base = (txn_id, SettlementKind.SETTLED)
    amounts = (payment.amount, None, payment.fee, None)
    if carried_days is not None:
        if carried_days >= max_carry_days:
            return Break(*base, Cause.TIMING_OVERDUE, *amounts, f"still not settled after {carried_days} days")
        return Break(*base, Cause.TIMING, *amounts, f"waiting for the next report (day {carried_days + 1})")
    if payment.captured_at >= cutoff_instant(business_date, cutoff):
        return Break(*base, Cause.TIMING, *amounts, f"captured after the {cutoff:%H:%M} cutoff; settles the next day")
    return Break(*base, Cause.LEDGER_ONLY_UNEXPLAINED, *amounts, "captured before the cutoff and not in the report")
```

A payment of ours is not in today's report. `base` and `amounts` are tuples reused to build the `Break`; `Break(*base, cause, *amounts, detail)` unpacks them with `*` into the arguments. If the transaction has already been carried: `TIMING_OVERDUE` once `carried_days` reaches the allowed maximum, otherwise `TIMING` with the day number. If it is new: a capture at or after the cutoff is `TIMING` (it settles tomorrow; `{cutoff:%H:%M}` formats the time of day). A capture before the cutoff that is still missing is `LEDGER_ONLY_UNEXPLAINED`, a human problem.

```python
def _total(payments: list[LedgerPayment]) -> Money | None:
    return payments[0].amount if payments else None
```

A tiny helper: the amount of the first payment, or `None` for an empty list.

```python
def _classify_refunds(
    txn_id: str, ours: Money | None, theirs: list[SettlementRow], currency: str
) -> Break | Matched | None:
    kind = SettlementKind.REFUNDED
    provider_total = -Money(sum((row.amount.minor for row in theirs), 0), currency) if theirs else None
    ledger_total = ours if ours is not None and ours.is_positive() else None
    if provider_total is not None and ledger_total is None:
        return Break(
            txn_id, kind, Cause.PROVIDER_ONLY_REFUND, None, provider_total, None, None,
            f"the provider refunded {provider_total.format()}, the ledger shows no refund",
        )  # fmt: skip
    if ledger_total is not None and provider_total is None:
        return Break(
            txn_id, kind, Cause.REFUND_ONLY_IN_LEDGER, ledger_total, None, None, None,
            f"the ledger shows a refund of {ledger_total.format()}, the provider's report has none",
        )  # fmt: skip
    if ledger_total is None or provider_total is None:
        return None
    if ledger_total != provider_total:
        return Break(
            txn_id, kind, Cause.REFUND_AMOUNT_MISMATCH, ledger_total, provider_total, None, None,
            f"ledger refunded {ledger_total.format()}, provider refunded {provider_total.format()}",
        )  # fmt: skip
    return Matched(txn_id, kind, ledger_total)
```

Compares refunds. `provider_total` is the provider's refunds summed and sign-flipped to a positive amount (`-Money(...)`), or `None` if there are none; `ledger_total` is our recorded refund if it is positive. Four cases: the provider refunded and we show none (`PROVIDER_ONLY_REFUND`); we show one and the provider's report has none (`REFUND_ONLY_IN_LEDGER`); neither side has one (`None`, nothing to report); both have one but the amounts differ (`REFUND_AMOUNT_MISMATCH`). Equal refunds give a `Matched`.

## Try it yourself

1. Reconcile two tiny sides by hand:

   ```bash
   .venv/bin/python -c "
   from datetime import UTC, date, datetime, time
   from payments_reconciliation.domain.money import Money
   from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, SettlementRow
   from payments_reconciliation.domain.reconciliation import LedgerView, reconcile
   row = SettlementRow('ptx_A1', SettlementKind.SETTLED, Money(4250,'USD'), Money(123,'USD'), datetime(2026,10,20,9,0,tzinfo=UTC))
   report = SettlementReport(date(2026,10,20), 'USD', (row,))
   result = reconcile(LedgerView(()), report, carried_days={}, cutoff=time(18,0), max_carry_days=2)
   print([(b.provider_txn_id, b.cause.value, b.handling.value) for b in result.breaks])"
   ```

   Expected: `[('ptx_A1', 'missing_capture', 'from_report')]`, because the ledger is empty but the provider settled it.

2. Read a settlement file format: `.venv/bin/python -c "from payments_reconciliation.domain.settlement import HEADER; print(','.join(HEADER))"` prints `provider_txn_id,amount,fee,status,settled_at`.

3. Try a bad amount: `.venv/bin/python -c "from payments_reconciliation.domain.money import Money; Money.parse('42.505','USD')"` raises `InvalidMoneyError: '42.505' has more than 2 decimal places for USD`.

## Mistakes people make

- **Reading `is` as `==`.** `is` asks whether two names are the very same object; the code uses it on purpose for enum members.
- **Forgetting that dataclasses here are frozen.** `payment.fee = ...` raises an error; use `with_fee`, which returns a new `LedgerPayment`.
- **Treating all differences as one kind.** Each cause has a different remedy; a shared "mismatch" bucket hides which.
- **Rounding with floats.** Money and fees are integers; the fee formula adds half the divisor before an integer division.
- **Skipping a bad CSV line quietly.** `parse_csv` raises with the line number so a corrupt report is never half-read.

## Check yourself

1. Why is `reconcile` a pure function? *It reads only its arguments and returns a result: no clock, database or file, so it is trivial to test.*
2. What decides between `TIMING` and `LEDGER_ONLY_UNEXPLAINED`? *Whether the capture happened at or after the provider's cutoff time.*
3. Which causes are cleared without a person? *Timing (carried), fee discrepancy (automatic), and missing capture, duplicate on our side and provider-only refund (fixed from the report).*
4. What does `Break.handling` do? *Looks the cause up in the `HANDLING` table.*
5. What happens to a settlement row with a negative fee? *`SettlementRow.__post_init__` raises `SettlementFormatError`.*

## Further reading

- Python 3.12 `dataclasses`: https://docs.python.org/3.12/library/dataclasses.html
- Python 3.12 `csv`: https://docs.python.org/3.12/library/csv.html
- Python 3.12 `enum`: https://docs.python.org/3.12/library/enum.html
- Python 3.12 `re`: https://docs.python.org/3.12/library/re.html
- Python 3.12 `collections.defaultdict`: https://docs.python.org/3.12/library/collections.html#collections.defaultdict
