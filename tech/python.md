# Python

## What it is

Python is a programming language. You write instructions in text files that end in `.py`, and a program called the **interpreter** (`python`) reads them and carries them out one by one. It is the language every file under `src/` and `tests/` in this repository is written in.

A **module** is one `.py` file. A **package** is a folder of modules with an `__init__.py` file in it. Here `src/payments_reconciliation/` is the package, and `domain`, `application`, `infrastructure` and `interfaces` are sub-packages.

## Why this repository uses it

The lesson is a daily job: read the provider's settlement file, compare it with the ledger, label each difference with its cause, fix what is safe to fix and report. Python reads files and text well, and its type hints let a checker (mypy) catch mistakes before the job ever runs. The matching rules (`domain/reconciliation.py`) are plain functions of their inputs: no clock, no database, no files. That makes them easy to test with a few lines.

## Where it appears

| Path | What it does with Python |
| --- | --- |
| `src/payments_reconciliation/domain/` | Pure rules: `Money`, ledger entries, the settlement report, the matching and the classification by cause. Standard library only. |
| `src/payments_reconciliation/application/` | The use cases (reconcile a day, resolve breaks, build the report) and the "ports" they need, written as `Protocol` classes. |
| `src/payments_reconciliation/infrastructure/` | PostgreSQL, the settlement files on disk, the stub provider, the day simulator, settings, logging, retry. |
| `src/payments_reconciliation/interfaces/` | The command-line program, the HTTP API, the server launcher, the health command. |
| `tests/` | The tests, also Python. |
| `pyproject.toml` | `requires-python = ">=3.12"`. |
| `Dockerfile` | Builds on the image `python:3.12.8-slim-bookworm`. |
| `Makefile` | Picks `python3.13`, else `python3.12`, else `python3` to create the virtual environment. |

## How it works underneath

1. You start `python -m some.module`. The interpreter finds the module on its **import path**, a list of folders.
2. It compiles the text to **bytecode** (a compact list of simple steps) and runs it on a small virtual machine. In memory, every value is an **object** with a type.
3. `import x` runs module `x` once and keeps the result in a dictionary called `sys.modules`. A second `import x` reuses it.
4. **Type hints** such as `amount: Money` are notes. The interpreter does not check them. A separate tool, mypy, reads them (see `mypy.md`).
5. Python has a **global interpreter lock** (GIL): only one thread runs Python bytecode at a time. Threads still help when they wait on the database or the network.

## Walkthrough of this repository's use

### The file header

```python
from __future__ import annotations
```

At the top of almost every module. It makes type hints be stored as text instead of being evaluated when the module loads, so a class can name itself in a hint and import cycles do not bite.

### Dataclasses: values that cannot change

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
```

A **dataclass** (standard library `dataclasses`) writes the boring methods for you: `__init__`, `__repr__` and `__eq__`. `frozen=True` makes instances **immutable**, so a finding cannot be edited after the matcher produced it. `slots=True` stores the fields in a fixed layout, which saves memory and rejects typos in attribute names. `Money | None` means "a `Money`, or nothing": a transaction that only the provider knows has no ledger amount.

`Money`, `LedgerPayment`, `SettlementRow`, `SettlementReport`, `MatchResult` and the others are built the same way.

### Money is an integer, never a float

`Money(4250, "USD")` means 42.50 US dollars: **4250 minor units** (cents). A `__post_init__` method, which runs right after the generated `__init__`, refuses a non-integer amount and an unsupported currency. `isinstance(self.minor, bool)` is checked first because `bool` is a subclass of `int` in Python (`True == 1`). Reading text uses `decimal.Decimal`, which stores decimal digits exactly (`Decimal("42.50")`), and refuses `42.505` for a two-decimal currency. Operators are defined (`__add__`, `__sub__`, `__neg__`, `__lt__`, ...) and each refuses to mix currencies.

### Enums with meaning

```python
class Cause(StrEnum):
    TIMING = "timing"
    FEE_DISCREPANCY = "fee_discrepancy"
    MISSING_CAPTURE = "missing_capture"
    ...

HANDLING: Mapping[Cause, Handling] = {
    Cause.TIMING: Handling.CARRY,
    Cause.FEE_DISCREPANCY: Handling.AUTOMATIC,
    Cause.AMOUNT_MISMATCH: Handling.HUMAN,
    ...
}
```

An **enum** is a fixed set of named values. `StrEnum` (Python 3.11 and newer) makes each member also a real string, so `Cause.TIMING == "timing"` and it can be stored in the database as text. `HANDLING` is a dictionary from each cause to what is done about it: wait (`CARRY`), fix with a rule (`AUTOMATIC`), fix from the report's data (`FROM_REPORT`) or give to a person (`HUMAN`). Every cause has its own label; there is no shared "mismatch" bucket.

### Grouping and joining in plain Python

```python
ours: dict[str, list[LedgerPayment]] = defaultdict(list)
for payment in ledger.payments:
    if payment.active:
        ours[payment.provider_txn_id].append(payment)
...
for txn_id in sorted(set(ours) | set(settled)):
```

`defaultdict(list)` is a dictionary that creates an empty list the first time you use a missing key, so `ours[txn].append(x)` just works. `set(ours) | set(settled)` is the **union** of the two key sets: every provider id that either side knows. `sorted(...)` makes the order deterministic, so two runs on the same input give the same output.

### Unpacking and pattern helpers

```python
base = (txn_id, SettlementKind.SETTLED)
amounts = (payment.amount, None, payment.fee, None)
return Break(*base, Cause.TIMING_OVERDUE, *amounts, f"still not settled after {carried_days} days")
```

`*base` spreads a tuple into separate arguments. Here it avoids repeating the id, the kind and four amounts in each of three similar `return` lines. `# fmt: skip` after a few multi-line calls tells the formatter to leave the author's layout alone.

### Dates and times

```python
def cutoff_instant(business_date: date, cutoff: time) -> datetime:
    return datetime.combine(business_date, cutoff, tzinfo=UTC)
```

`date` is a calendar day, `time` a clock time, `datetime` both. A **timezone-aware** datetime (with `tzinfo=UTC`) is a precise instant; a naive one is ambiguous. `SettlementRow` refuses a naive `settled_at`. A capture at or after the day's 23:00 UTC cutoff counts for the next day's report: that is how a **timing difference** is recognized.

### Files, atomically

```python
temporary = path.with_suffix(".csv.part")
temporary.write_text(text, encoding="utf-8")
os.replace(temporary, path)
```

`pathlib.Path` represents file paths as objects (`directory / "settlement_2026-10-19.csv"`). Writing to a `.part` file and then `os.replace`-ing it over the real name is **atomic**: a reader sees the old file or the whole new file, never half of one. `read_text` raises `FileNotFoundError`, which the code turns into the named `SettlementMissingError`.

### Protocols: the "ports"

```python
class SettlementFiles(Protocol):
    def read(self, business_date: date) -> str: ...
    def write(self, business_date: date, text: str) -> None: ...
```

A `Protocol` describes a shape: "anything with these methods". The use cases depend on these shapes (in `application/ports.py`); the real adapters (`DirectorySettlementFiles`, the PostgreSQL classes) and the test fakes (`tests/unit/fakes.py`) both fit.

### Reproducible randomness

```python
rng = random.Random(plan.seed)
```

The simulator that invents a day of 500 payments uses its own `Random` object seeded with a number, so the same seed always gives the same day. The planted breaks are therefore the same every run.

### Context managers, exceptions, generics

`with self._uow_factory() as uow:` opens a database transaction and commits or rolls back on exit (`@contextmanager`, see `psycopg.md`). `raise X(...) from error` keeps the original error as the cause. `def retry[T](...)` (Python 3.12 syntax) declares a type variable for the generic retry helper.

### The command-line program

```python
parser = argparse.ArgumentParser(prog="payments-reconciliation", description=__doc__)
commands = parser.add_subparsers(dest="command", required=True)
simulate = commands.add_parser("simulate-day", help="run a simulated day through the capture path")
simulate.add_argument("--date", type=parse_date, required=True)
```

`argparse` (standard library) reads the command line. `add_subparsers` makes `payments-reconciliation simulate-day`, `reconcile`, `resolve`, `report`, `worklist` and `demo` into separate commands with their own options. `type=parse_date` converts and validates the text with a function of ours (`date.fromisoformat`, with a clear error). A dictionary `HANDLERS: dict[str, Callable[...]]` maps each command name to its function, and `main` returns an exit status: 0 success, 1 a rule was broken or the input was wrong, 2 a configuration error.

### The program entry pattern

```python
if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
```

Python sets `__name__` to `"__main__"` only when the file is run directly. `sys.exit(code)` ends the program with that exit status; `0` means success. The commands installed from `pyproject.toml` (`payments-reconciliation`, `-serve`, `-health`) call `main()` the same way.

## Try it yourself

1. Parse an amount:

   ```bash
   .venv/bin/python -c "from payments_reconciliation.domain.money import Money; m = Money.parse('42.50', 'USD'); print(m, m.format())"
   ```

   Expected: `Money(minor=4250, currency='USD') 42.50`.

2. Classify one difference by hand:

   ```bash
   .venv/bin/python - <<'EOF'
   from datetime import UTC, date, datetime, time
   from uuid import UUID
   from payments_reconciliation.domain.money import Money
   from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState
   from payments_reconciliation.domain.reconciliation import LedgerView, reconcile
   from payments_reconciliation.domain.settlement import SettlementKind, SettlementReport, SettlementRow
   usd = "USD"
   ours = LedgerPayment(UUID(int=1), "ptx_0112", Money(4250, usd), Money(123, usd), PaymentState.CAPTURED,
                        PaymentSource.SERVICE, datetime(2026, 10, 20, 12, tzinfo=UTC), Money(0, usd))
   theirs = SettlementRow("ptx_0112", SettlementKind.SETTLED, Money(4250, usd), Money(141, usd),
                          datetime(2026, 10, 20, 23, 30, tzinfo=UTC))
   result = reconcile(LedgerView((ours,)), SettlementReport(date(2026, 10, 20), usd, (theirs,)),
                      carried_days={}, cutoff=time(23, 0), max_carry_days=2)
   for b in result.breaks:
       print(b.provider_txn_id, b.cause.value, b.handling.value, b.detail)
   EOF
   ```

   Expected: `ptx_0112 fee_discrepancy automatic fee recorded 1.23, settled 1.41`.

3. See that every cause has a handling:

   ```bash
   .venv/bin/python -c "from payments_reconciliation.domain.reconciliation import HANDLING, Cause; print(len(Cause), len(HANDLING), HANDLING[Cause.AMOUNT_MISMATCH])"
   ```

   Expected: `11 11 human`.

4. Watch immutability refuse a change:

   ```bash
   .venv/bin/python -c "from payments_reconciliation.domain.money import Money; m = Money(1, 'USD'); m.minor = 2"
   ```

   Expected: a `FrozenInstanceError`.

5. Read the command-line help: `.venv/bin/payments-reconciliation --help`. Expected: the six sub-commands.

## Mistakes people make

- **Using floats for money.** `0.1 + 0.2` is not `0.3`. This repository stores integers and uses `Decimal` only to read text.
- **Mixing naive and aware datetimes.** Python refuses to compare them (`TypeError`); this code makes every instant UTC-aware.
- **Joining on amount and time.** The rows could pair up wrongly; the matcher joins on the provider's stable id only.
- **Mutable default arguments** (`def f(x=[])`): the list is shared by every call. The code uses `field(default_factory=...)`.
- **Catching everything** (`except:`). The code catches named errors only.

## Check yourself

1. What does `frozen=True` on a dataclass do, and why is it good for a `Break`? *It makes instances read-only, so a finding cannot change after it was produced.*
2. Why is `isinstance(x, bool)` checked before `isinstance(x, int)`? *`bool` is a subclass of `int`, so `True` would pass as the number 1.*
3. What does `set(a) | set(b)` give? *Every key that appears in either collection.*
4. Why write a file to a `.part` name and then `os.replace` it? *So a reader never sees a half-written file.*
5. Why does the simulator use `random.Random(seed)`? *The same seed gives the same simulated day, so tests and demos repeat exactly.*

## Further reading

- Python 3.12 tutorial and reference: https://docs.python.org/3.12/
- `dataclasses`: https://docs.python.org/3.12/library/dataclasses.html
- `enum` (`StrEnum`): https://docs.python.org/3.12/library/enum.html
- `datetime`: https://docs.python.org/3.12/library/datetime.html
- `pathlib`: https://docs.python.org/3.12/library/pathlib.html
- `argparse`: https://docs.python.org/3.12/library/argparse.html
- `typing.Protocol`: https://docs.python.org/3.12/library/typing.html#typing.Protocol
