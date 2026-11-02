# Python walkthrough: the infrastructure package, line by line

## What it is

This note reads every line of the `infrastructure` package: settings read from the environment, logging, the retry helper, the clock and id source, the settlement files on disk, the stub provider, the day simulator, and the PostgreSQL pool, unit of work and repositories.

An **adapter** is code that connects the program to something outside it. Every adapter here implements a port that the application layer defined.

## Why this repository uses it

Everything that touches the outside world lives in one layer, so the rules above it stay testable without a database. Here that also includes the simulator, which makes the six differences of the lesson happen the way they do in production: by a late capture, by the provider settling other numbers, by a process dying at the wrong moment, by a retry recording twice, and by a refund the service never heard about.

## Where it appears

| File | What it is |
| --- | --- |
| `src/payments_reconciliation/infrastructure/__init__.py` | The infrastructure package. |
| `src/payments_reconciliation/infrastructure/system.py` | The real clock and id source. |
| `src/payments_reconciliation/infrastructure/retry.py` | Retry with exponential backoff and jitter. |
| `src/payments_reconciliation/infrastructure/logging.py` | Structured log lines with card masking. |
| `src/payments_reconciliation/infrastructure/settings.py` | Configuration from the environment. |
| `src/payments_reconciliation/infrastructure/files.py` | Settlement files on disk. |
| `src/payments_reconciliation/infrastructure/stub_provider.py` | The in-memory payment provider. |
| `src/payments_reconciliation/infrastructure/simulation.py` | The day simulator with the planted breaks. |
| `src/payments_reconciliation/infrastructure/postgres.py` | The PostgreSQL adapters. |

## How it works underneath

**Settings** are read once at start and validated, and a bad value stops the program with a message naming the variable. **PostgreSQL access** goes through psycopg 3 and a pool: a unit of work borrows one connection, which is one transaction, and returns it. **Advisory locks** let code take a lock on a number derived from a text so related work takes turns. **Pipeline mode** sends several statements before waiting for answers. **The settlement file** is written to a temporary name and renamed, which the operating system does atomically.

## Walkthrough of this repository's use



### `src/payments_reconciliation/infrastructure/__init__.py` (1 lines)

```python
"""Adapters: PostgreSQL, the settlement files, the stub provider, the day simulator, settings, logging."""
```

The docstring of the `infrastructure` package. An **adapter** is a piece of code that connects the program to something outside it: the PostgreSQL database, the settlement files, the stand-in provider, the simulator of a business day, the environment variables and the log output.

### `src/payments_reconciliation/infrastructure/system.py` (23 lines)

```python
"""The real clock and the real id source."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID
```

The two real implementations of the `Clock` and `IdSource` ports. `uuid` is the standard library module for universally unique identifiers (128-bit labels that will not collide in practice). `datetime.UTC` is the UTC time zone object.

```python
class SystemClock:
    """The current UTC time."""

    def now(self) -> datetime:
        """Now, timezone-aware."""
        return datetime.now(UTC)
```

`SystemClock.now()` returns `datetime.now(UTC)`: the current time as a **timezone-aware** value (it knows it is UTC). A "naive" datetime has no zone and is ambiguous; every instant in this program is aware. The class has no constructor and no state; it only has to have a `now` method to satisfy the `Clock` protocol.

```python
class RandomIds:
    """Random version-4 UUIDs."""

    def new_id(self) -> UUID:
        """A fresh UUID."""
        return uuid.uuid4()
```

`RandomIds.new_id()` returns `uuid.uuid4()`, a random version-4 UUID. Because ids come from the injected source, tests can use a sequential one and get predictable ids.

### `src/payments_reconciliation/infrastructure/retry.py` (38 lines)

```python
"""Retry with exponential backoff and jitter for calls that cross the network."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
```

A generic helper that calls a function again after a failure, waiting longer each time. The imports: `random` (for the jitter, below), `time` (for `sleep`) and `Callable`.

```python
def retry[T](
    operation: Callable[[], T],
    *,
    attempts: int,
    base_delay_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    max_delay_seconds: float = 10.0,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Call `operation` up to `attempts` times, doubling the pause after each failure in `retry_on`.

    The pause is `base * 2**(attempt - 1)` capped at `max_delay_seconds`, then scaled by a random
    factor in [0.5, 1.0] so many clients do not retry in step. The last failure is raised.
    """
```

`def retry[T](...)` declares a function with a **type variable** `T` (Python 3.12 syntax). It says: whatever type `operation` returns, `retry` returns the same type, so the checker can follow the value through. The bare `*` makes every later parameter keyword-only. The parameters: `attempts` (the maximum number of calls), `base_delay_seconds` (the first pause), `retry_on` (a tuple of the error types worth retrying), `sleep` (the function that waits, replaceable in tests so they do not really wait), `rng` (a random number generator, replaceable so tests are repeatable), `max_delay_seconds` (a cap on any pause) and `on_retry` (an optional function told about each retry, used for logging).

```python
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    jitter = rng or random.Random()
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except retry_on as error:
            if attempt == attempts:
                raise
            if on_retry:
                on_retry(attempt, error)
            sleep(min(max_delay_seconds, base_delay_seconds * 2 ** (attempt - 1)) * jitter.uniform(0.5, 1.0))
    raise AssertionError("unreachable")  # pragma: no cover
```

The body. `attempts < 1` is refused with `ValueError` because "call at most zero times" makes no sense. `jitter = rng or random.Random()` uses the supplied generator or makes a fresh one. `for attempt in range(1, attempts + 1):` loops attempt numbers 1, 2, ... up to `attempts`. Inside, `try: return operation()` calls the function and returns its result on success. `except retry_on as error:` catches only the listed error types; any other error flies out untouched. If this was the last attempt, `raise` (alone) re-raises the same error to the caller. Otherwise `on_retry` is told, and the code sleeps for `min(max_delay_seconds, base_delay_seconds * 2 ** (attempt - 1)) * jitter.uniform(0.5, 1.0)`: the base delay doubles with every attempt (that is **exponential backoff**, `2 ** n` is two to the power n), is capped, and is then multiplied by a random factor between 0.5 and 1.0 (**jitter**), so many clients that failed together do not all retry at the same instant. The final `raise AssertionError("unreachable")` is a guard for the type checker; the `# pragma: no cover` comment tells the coverage tool not to count it.

### `src/payments_reconciliation/infrastructure/logging.py` (55 lines)

```python
"""Structured logging: one logfmt line per event, with anything shaped like a card number masked."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

# 13 to 19 digits, optionally grouped by spaces or dashes: the shape of a card number (a published test number too).
```

Structured logging. The docstring: one **logfmt** line per event (a line of `key=value` pairs separated by spaces), with anything shaped like a card number masked. Imports: `json` (used to quote values), `logging`, `re`, `sys`, `datetime`/`UTC` and typing helpers.

```python
_CARD_NUMBER = re.compile(r"(?<![\w.])(?:\d[ -]?){13,19}(?![\w])")
_SECRET_FIELDS = {"card_token", "card_number", "cvv", "password", "authorization"}
```

A **regular expression** that matches the shape of a card number: 13 to 19 digits, optionally separated by single spaces or dashes. The pieces: `(?<![\w.])` means "not preceded by a letter, digit, underscore or dot" (a **lookbehind**, so digits inside a longer word or a price such as `12.3456789012345` are not matched); `(?:\d[ -]?)` is one digit optionally followed by a space or dash (the `?:` makes a group that is not captured); `{13,19}` repeats it 13 to 19 times; `(?![\w])` means "not followed by a word character" (a **lookahead**). The comment notes that a published test card number would match too. `_SECRET_FIELDS` lists field names whose values are always withheld.

```python
def redact(text: str) -> str:
    """Mask anything that looks like a card number. Defense in depth: the code never logs card data to begin with."""
    return _CARD_NUMBER.sub("[redacted-number]", text)
```

`redact` replaces every card-number-shaped run of digits in `text` with `[redacted-number]` using `_CARD_NUMBER.sub(...)`. The docstring calls it defense in depth: the code never logs card data to begin with.

```python
def _quote(value: object) -> str:
    text = redact(str(value))
    return json.dumps(text) if not text or any(char in text for char in ' ="\n') else text
```

`_quote` prepares a value for a logfmt line. It converts the value to text and redacts it. If the text is empty or contains a space, an equals sign, a double quote or a newline (`any(char in text for char in ' ="\n')` is a **generator expression** inside `any`), it is wrapped in double quotes by `json.dumps(text)`, which also escapes quotes and newlines. Otherwise it is returned as it is. This makes every line splittable into `key=value` pairs without ambiguity.

```python
class LogfmtFormatter(logging.Formatter):
    """Format a log record as one logfmt line, with the record's extra fields as key=value pairs."""
```

A **formatter** decides what a log record looks like as text. This one subclasses the standard `logging.Formatter` and overrides `format`.

```python
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        parts = [
            f"ts={timestamp}",
            f"level={record.levelname}",
            f"logger={record.name}",
            f"msg={_quote(record.getMessage())}",
        ]
        extra: dict[str, Any] = getattr(record, "fields", {})
        parts.extend(
            f"{key}={'[withheld]' if key in _SECRET_FIELDS else _quote(value)}" for key, value in extra.items()
        )
        if record.exc_info:
            parts.append(f"exc={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)
```

`record.created` is the time the event happened as a number of seconds since 1970; `datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")` turns it into text such as `2026-10-27T12:00:00.123+00:00`. The line starts with four pairs: `ts`, `level` (such as `INFO`), `logger` (the name of the module) and `msg` (`record.getMessage()`, quoted). Then the extra fields: `getattr(record, "fields", {})` fetches the dictionary the `fields(...)` helper attached (or an empty one). Each pair is written `key=value`; if the key is in `_SECRET_FIELDS` the value is the fixed text `[withheld]`. If the record carries an exception (`record.exc_info`), the traceback text is added as `exc=...`. The parts are joined with single spaces.

```python
def configure_logging(level: str, stream: TextIO | None = None) -> None:
    """Send every log record to `stream` (stderr by default) as logfmt. Safe to call twice."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(max(logging.getLevelName(level), logging.WARNING))
```

`configure_logging` sets up logging for the whole program. It creates a `StreamHandler` writing to the given stream (standard error by default, `sys.stderr`), gives it the formatter, and makes it the **only** handler of the **root logger** (the ancestor of all loggers): `root.handlers[:] = [handler]` replaces the list contents, so calling the function twice does not print every line twice. `root.setLevel(level)` sets the minimum level that is shown (DEBUG, INFO, WARNING or ERROR). The last loop quiets two noisy libraries (`uvicorn.access` and `psycopg.pool`) to at least WARNING.

### `src/payments_reconciliation/infrastructure/settings.py` (120 lines)

```python
"""Configuration from the environment, validated once at startup (twelve-factor). Secrets only from here."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from payments_reconciliation.domain.money import CURRENCY_EXPONENTS
```

Configuration from the **environment**. The docstring names the rule (twelve-factor: settings come from environment variables, and secrets come only from there). `os` reads the environment; `Mapping` is a read-only dictionary type; `dataclass` and `field` build the settings objects; `time` is a time of day, `Path` a file path; `CURRENCY_EXPONENTS` is the table of supported currencies from `money.py`.

```python
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
```

The accepted log levels, as a set.

```python
class ConfigurationError(ValueError):
    """An environment variable is missing or has an unusable value."""
```

A named error for a missing or unusable setting; it subclasses `ValueError`.

```python
def _text(environ: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = environ.get(name, default if default is not None else "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set and not empty")
    return value
```

`_text` reads a text setting. `environ.get(name, default if default is not None else "")` takes the variable or the default (or an empty string), `.strip()` removes spaces at both ends, and an empty result raises `ConfigurationError(f"{name} must be set and not empty")`. A setting with no default, such as the password, therefore fails loudly when it is missing.

```python
def _positive_int(environ: Mapping[str, str], name: str, default: int, *, maximum: int | None = None) -> int:
    raw = environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}")
    if maximum is not None and int(raw) > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}, got {raw}")
    return int(raw)
```

`_positive_int` reads a whole-number setting. `raw.isdigit()` is true for text made only of the digits 0-9; a value that is not digits or is below 1 is refused; if `maximum` is given a larger value is refused too. The `*` makes `maximum` keyword-only.

```python
def _clock_time(environ: Mapping[str, str], name: str, default: str) -> time:
    raw = environ.get(name, default).strip()
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be HH:MM in UTC, got {raw!r}") from error
```

`_clock_time` reads a time of day written `HH:MM` in UTC. `raw.split(":")` splits the text at the colon into two parts, which are assigned to `hour` and `minute` and turned into numbers for `time(...)`. Any `ValueError` (wrong number of parts, not a number, out of range) becomes a `ConfigurationError` that names the variable.

```python
@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """The PostgreSQL part of the configuration; the password is never printed."""

    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)  # never printed or logged
    connect_timeout_seconds: int
    pool_max_size: int
```

`PostgresSettings` holds the database settings. `password: str = field(repr=False)` marks the password field as left out of the printed form of the object (`repr`), so printing or logging the settings can never reveal it. The other fields are the host, port, database, user, connect timeout and the largest size of the connection pool.

```python
@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Where the HTTP API listens and how long it waits for in-flight requests when told to stop."""

    host: str
    port: int
    shutdown_grace_seconds: int
```

`ApiSettings`: where the HTTP API listens (`host`, `port`) and how many seconds it waits for in-flight requests at shutdown.

```python
@dataclass(frozen=True, slots=True)
class ReconciliationSettings:
    """The rules of the daily job."""

    currency: str
    settlement_dir: Path
    cutoff: time
    max_carry_days: int
    provider_fee_basis_points: int
```

`ReconciliationSettings`: the rules of the daily job: the currency, the folder of settlement files, the provider's cutoff time of day, how many days a timing difference may be carried, and the fee the stub provider charges, in basis points.

```python
@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the programs read from the environment. See .env.example for each variable."""

    postgres: PostgresSettings
    api: ApiSettings
    reconciliation: ReconciliationSettings
    log_level: str
```

`Settings` groups everything the programs read from the environment: the database, the API, the reconciliation rules and the log level.

```python
    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Read and validate; raises `ConfigurationError` naming the first bad variable."""
        env = os.environ if environ is None else environ
        level = _text(env, "LOG_LEVEL", "INFO").upper()
        if level not in _LEVELS:
            raise ConfigurationError(f"LOG_LEVEL must be one of {sorted(_LEVELS)}, got {level!r}")
        currency = _text(env, "CURRENCY", "USD").upper()
        if currency not in CURRENCY_EXPONENTS:
            raise ConfigurationError(f"CURRENCY must be one of {sorted(CURRENCY_EXPONENTS)}, got {currency!r}")
        return cls(
            postgres=PostgresSettings(
                host=_text(env, "POSTGRES_HOST", "localhost"),
                port=_positive_int(env, "POSTGRES_PORT", 54612, maximum=65535),
                database=_text(env, "POSTGRES_DB", "reconciliation"),
                user=_text(env, "POSTGRES_USER", "reconciliation"),
                password=_text(env, "POSTGRES_PASSWORD"),
                connect_timeout_seconds=_positive_int(env, "POSTGRES_CONNECT_TIMEOUT_SECONDS", 5),
                pool_max_size=_positive_int(env, "DB_POOL_MAX_SIZE", 10),
            ),
            api=ApiSettings(
                host=_text(env, "API_HOST", "127.0.0.1"),
                port=_positive_int(env, "API_PORT", 8462, maximum=65535),
                shutdown_grace_seconds=_positive_int(env, "SHUTDOWN_GRACE_SECONDS", 10),
            ),
            reconciliation=ReconciliationSettings(
                currency=currency,
                settlement_dir=Path(_text(env, "SETTLEMENT_DIR", "var/settlement")),
                cutoff=_clock_time(env, "SETTLEMENT_CUTOFF_UTC", "23:00"),
                max_carry_days=_positive_int(env, "MAX_CARRY_DAYS", 2),
                provider_fee_basis_points=_positive_int(env, "PROVIDER_FEE_BPS", 290, maximum=10_000),
            ),
            log_level=level,
        )
```

`from_env` is a class method (`@classmethod` on the line above; `cls` is the class itself) that builds the settings. It takes the environment (default `os.environ`, the real one; tests pass a plain dictionary). It first reads and checks `LOG_LEVEL` (upper-cased) and `CURRENCY` (default `USD`, must be a supported currency; `sorted(...)` in the message lists the allowed ones). Then every group is filled from named variables with defaults: `POSTGRES_HOST` (`localhost`), `POSTGRES_PORT` (54612, at most 65535), `POSTGRES_DB` and `POSTGRES_USER` (`reconciliation`), `POSTGRES_PASSWORD` (no default), `POSTGRES_CONNECT_TIMEOUT_SECONDS` (5), `DB_POOL_MAX_SIZE` (10); `API_HOST` (`127.0.0.1`), `API_PORT` (8462), `SHUTDOWN_GRACE_SECONDS` (10); `SETTLEMENT_DIR` (`var/settlement`), `SETTLEMENT_CUTOFF_UTC` (`23:00`), `MAX_CARRY_DAYS` (2) and `PROVIDER_FEE_BPS` (290, at most 10,000; `10_000` is the same number with a readability underscore). The docstring says it raises `ConfigurationError` naming the first bad variable.

### `src/payments_reconciliation/infrastructure/files.py` (36 lines)

```python
"""Settlement files on disk, one per business day: `settlement_YYYY-MM-DD.csv`."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from payments_reconciliation.domain.errors import SettlementMissingError
```

Settlement files on disk, one per business day, named `settlement_YYYY-MM-DD.csv`. `os` is used for an atomic rename; `Path` is a file path.

```python
class DirectorySettlementFiles:
    """`SettlementFiles` over a directory. Writes go to a temporary file and are renamed (no half files)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
```

The `SettlementFiles` port over a folder. The docstring promises that writes go to a temporary file and are renamed, so a reader never sees a half-written file.

```python
    def path_for(self, business_date: date) -> Path:
        """Where the file for a day lives."""
        return self._directory / f"settlement_{business_date.isoformat()}.csv"
```

Where the file for a day lives: the folder joined (`/` joins paths) with `settlement_2026-10-20.csv` (`isoformat()` writes the date as `YYYY-MM-DD`).

```python
    def read(self, business_date: date) -> str:
        """The file's text, or `SettlementMissingError` when the provider's file has not arrived."""
        path = self.path_for(business_date)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise SettlementMissingError(f"no settlement file for {business_date.isoformat()} at {path}") from error
```

Reads the file as UTF-8 text. If the file is not there (`FileNotFoundError`) it raises `SettlementMissingError` naming the day and the path; `from error` keeps the original error as the cause.

```python
    def write(self, business_date: date, text: str) -> None:
        """Store the file for a day, replacing any earlier one."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(business_date)
        temporary = path.with_suffix(".csv.part")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
```

Stores a file. The folder is created if needed (`mkdir(parents=True, exist_ok=True)`). The text is written to `settlement_<date>.csv.part` (`with_suffix` swaps the ending), then `os.replace` renames it onto the final name in one step, which the operating system does atomically: a reader sees either the old file or the whole new one.

### `src/payments_reconciliation/infrastructure/stub_provider.py` (134 lines)

```python
"""The stub payment provider: deterministic, honors idempotency keys, and settles in daily batches.

It is the source of truth for what moved: captures and refunds are recorded here the moment they happen, and the
settlement report of a business day is computed from these records. A scenario can make the provider's settled
figures differ from what the payments service recorded (`adjust_fee`, `adjust_amount`), the way a real provider does
after a surcharge or a dispute adjustment.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from payments_reconciliation.application.ports import CaptureResult, RefundResult
from payments_reconciliation.domain.errors import PaymentNotFoundError
from payments_reconciliation.domain.fees import FeePolicy
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.settlement import SettlementKind, SettlementRow
```

The stub payment provider: deterministic, honors idempotency keys, and settles in daily batches. The docstring explains that it is the **source of truth** for what moved: captures and refunds are recorded here the moment they happen, and the settlement report of a business day is computed from these records. A scenario can make the provider's settled figures differ from what the payments service recorded (`adjust_fee`, `adjust_amount`), as a real provider does after a surcharge or a dispute. `threading` provides a lock; `timedelta` is a length of time.

```python
@dataclass(slots=True)
class _Capture:
    txn_id: str
    amount: Money
    fee: Money
    captured_at: datetime
    settled_amount: Money
    settled_fee: Money
```

A private record of one capture at the provider: its id, the amount and fee quoted at capture, when it happened, and the amount and fee it will **settle** with (they start equal and a scenario may change them). `@dataclass(slots=True)` writes the constructor.

```python
@dataclass(slots=True)
class _Refund:
    refund_id: str
    txn_id: str
    amount: Money
    refunded_at: datetime
```

A private record of one refund: its id, the transaction it refunds, the amount and the time.

```python
class StubProvider:
    """A `PaymentProvider` kept in memory. Thread-safe; ids are sequential so runs are repeatable."""
```

A `PaymentProvider` kept in memory. The docstring promises it is thread-safe (one lock) and that ids are sequential so runs are repeatable.

```python
    def __init__(self, fee_policy: FeePolicy, id_prefix: str = "ptx_") -> None:
        self._fee_policy = fee_policy
        self._lock = threading.RLock()
        self._prefix = id_prefix
        self._sequence = 0
        self._refund_sequence = 0
        self._captures: dict[str, _Capture] = {}
        self._refunds: list[_Refund] = []
        self._by_key: dict[tuple[str, str], object] = {}
        self.executed: dict[str, int] = {"capture": 0, "refund": 0}
        """How many times each operation really ran (a repeat of a key does not count)."""
```

Keeps the fee policy, a re-entrant lock, the id prefix and two counters (capture ids and refund ids), dictionaries of captures and refunds, `_by_key` (remembers the result for each (operation, key) pair so a repeat returns the first result) and `executed`, a count of how many times each operation really ran. The string below `executed` is an attribute docstring.

```python
    def start_series(self, id_prefix: str) -> None:
        """Begin a new run of transaction ids: `<prefix>0001`, `<prefix>0002`, ..."""
        with self._lock:
            self._prefix = id_prefix
            self._sequence = 0
```

Begins a new run of transaction ids: `<prefix>0001`, then `<prefix>0002`, one more each time, so each simulated day can have its own recognizable ids.

```python
    def capture(self, *, idempotency_key: str, amount: Money, occurred_at: datetime) -> CaptureResult:
        """Capture once per key and return the provider's own transaction id and fee."""
        with self._lock:
            cached = self._by_key.get(("capture", idempotency_key))
            if isinstance(cached, CaptureResult):
                return cached
            self._sequence += 1
            txn_id = f"{self._prefix}{self._sequence:04d}"
            fee = self._fee_policy.fee_for(amount)
            self._captures[txn_id] = _Capture(txn_id, amount, fee, occurred_at, amount, fee)
            result = CaptureResult(txn_id, fee)
            self._by_key[("capture", idempotency_key)] = result
            self.executed["capture"] += 1
            return result
```

Captures once per key. Under the lock, if the key was seen, the first result is returned (`isinstance` checks it is the right kind). Otherwise the sequence number goes up, the id is made like `ptx_0001` (`{self._sequence:04d}` pads to four digits), the fee comes from the policy, the capture is stored, the result is remembered under the key and `executed["capture"]` increases.

```python
    def refund(
        self, *, idempotency_key: str, provider_txn_id: str, amount: Money, occurred_at: datetime
    ) -> RefundResult:
        """Refund once per key, never more than was captured."""
        with self._lock:
            cached = self._by_key.get(("refund", idempotency_key))
            if isinstance(cached, RefundResult):
                return cached
            capture = self._captures.get(provider_txn_id)
            if capture is None:
                raise PaymentNotFoundError(f"the provider has no transaction {provider_txn_id}")
            already = sum((r.amount.minor for r in self._refunds if r.txn_id == provider_txn_id), 0)
            if amount.minor + already > capture.amount.minor:
                raise PaymentNotFoundError(f"refund of {amount.format()} exceeds the capture of {provider_txn_id}")
            self._refund_sequence += 1
            refund_id = f"ref_{self._refund_sequence:04d}"
            self._refunds.append(_Refund(refund_id, provider_txn_id, amount, occurred_at))
            result = RefundResult(refund_id)
            self._by_key[("refund", idempotency_key)] = result
            self.executed["refund"] += 1
            return result
```

Refunds once per key, never more than was captured. It looks up the capture (an unknown transaction raises `PaymentNotFoundError`), adds up earlier refunds of it (`sum(..., 0)` with a start value of 0), refuses a total above the captured amount, then records a refund with an id like `ref_0001`.

```python
    def adjust_fee(self, provider_txn_id: str, fee: Money) -> None:
        """The provider settles with a different fee than it quoted at capture (a surcharge applied later)."""
        with self._lock:
            self._captures[provider_txn_id].settled_fee = fee
```

A scenario hook: the provider settles with a different fee than it quoted at capture (a surcharge applied later).

```python
    def adjust_amount(self, provider_txn_id: str, amount: Money) -> None:
        """The provider settles a different amount than it captured (a dispute or conversion adjustment)."""
        with self._lock:
            self._captures[provider_txn_id].settled_amount = amount
```

A scenario hook: the provider settles a different amount than it captured (a dispute or conversion adjustment).

```python
    def settlement_rows(self, business_date: date, cutoff: time) -> list[SettlementRow]:
        """The rows of the batch the provider closes at `cutoff` on `business_date`.

        The batch holds everything captured or refunded since the previous day's cutoff, so a capture made after
        the cutoff settles in the next day's batch. Rows are in transaction-id order.
        """
        close = datetime.combine(business_date, cutoff, tzinfo=UTC)
        open_ = close - timedelta(days=1)
        stamp = close + timedelta(minutes=30)
        with self._lock:
            rows: list[SettlementRow] = []
            for capture in sorted(self._captures.values(), key=lambda item: item.txn_id):
                if open_ <= capture.captured_at < close:
                    rows.append(
                        SettlementRow(
                            capture.txn_id, SettlementKind.SETTLED, capture.settled_amount, capture.settled_fee, stamp
                        )
                    )
            for refund in self._refunds:
                if open_ <= refund.refunded_at < close:
                    currency = refund.amount.currency
                    rows.append(
                        SettlementRow(
                            refund.txn_id, SettlementKind.REFUNDED, -refund.amount, Money.zero(currency), stamp
                        )
                    )
            return sorted(rows, key=lambda row: (row.provider_txn_id, row.kind.value))
```

Builds the rows of the batch the provider closes at `cutoff` on `business_date`. `close` is that moment, `open_` is 24 hours earlier (the name has an underscore because `open` is a Python built-in) and `stamp` (30 minutes after the close) is the time written on every row. The docstring states the rule: the batch holds everything captured or refunded since the previous day's cutoff, so a capture made after the cutoff settles in the next day's batch. Under the lock, every capture in transaction-id order whose time is in `[open_, close)` becomes a `SETTLED` row with the **settled** amount and fee; every refund in the window becomes a `REFUNDED` row with a negative amount (`-refund.amount`) and a zero fee. The rows are sorted by transaction id and kind.

### `src/payments_reconciliation/infrastructure/simulation.py` (226 lines)

```python
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
```

A simulated day of payments, run through the real capture path, with the lesson's six breaks planted by cause. The docstring's list explains how each break arises exactly as it would in production: a capture after the provider's cutoff (timing); the provider settling a fee other than the quoted one; a process dying after the provider captured and before the ledger was written (missing capture); a retry that recorded the same provider transaction twice (duplicate); a refund the provider made that the service never heard about; and the provider settling an amount other than the one captured. Nothing is faked in the ledger or in the file. New imports: `random` (a seeded random generator), `uuid`, `monotonic` (a stopwatch clock) and `field`.

```python
class SimulatedCrashError(RuntimeError):
    """The process died between the provider's answer and the ledger write."""
```

Raised on purpose to imitate the process dying between the provider's answer and the ledger write.

```python
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
```

A clock the simulation sets to each payment's time. `set` moves it, `now` reads it. It satisfies the `Clock` port.

```python
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
```

Where, by position in the day's capture order, each break is planted (so the lesson's ids are `ptx_0050`, `ptx_0112`, `ptx_0200`, `ptx_0215`, `ptx_0300`, `ptx_0400`). The fields after the positions are the late capture time (23:40, after the cutoff), the fee the provider settles for the fee break (141) and the amount it settles for the mismatch (9999). Every field has a default, so `Plants()` is the lesson's setup.

```python
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
```

The captured amount at each planted position, in minor units: 50.00, 42.50 (the fee example), 60.00, 33.00, 25.00 and 95.00 (the mismatch).

```python
# The lesson's worked examples sit at fixed positions on every day.
FIXED_AMOUNTS = {1: 4250, 2: 1800}
```

Positions 1 and 2 of every day always have the amounts 42.50 and 18.00, the lesson's worked examples.

```python
@dataclass(frozen=True, slots=True)
class DayPlan:
    """One simulated day."""

    business_date: date
    payments: int
    seed: int
    id_prefix: str = "ptx_"
    plant_breaks: bool = False
    plants: Plants = field(default_factory=Plants)
```

One simulated day: the date, how many payments, the random seed, the id prefix, whether to plant breaks and the `Plants`. `field(default_factory=Plants)` makes a fresh `Plants` for each plan.

```python
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
```

What the simulation did, for the program to print: counts, the lists of affected ids and the elapsed seconds.

```python
class DaySimulator:
    """Run a `DayPlan` through `RecordCapture` and publish the provider's settlement file for the day."""
```

Runs a `DayPlan` through `RecordCapture` and publishes the provider's settlement file for the day.

```python
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
```

Keeps its collaborators; everything after `*` is keyword-only. `_crash_armed` is a flag that is true only while the payment that must crash is being captured.

```python
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
```

The main loop. It starts a stopwatch, starts the provider's id series, builds a `RecordCapture` whose hook is `_maybe_crash`, and a seeded random generator (the same seed gives the same day). For each position from 1 to the number of payments: it works out the time and sets the simulated clock, computes the amount, and makes the request key `capture-<date>-<prefix><position>`. The crash flag is armed only for the planted missing-capture position. `capture.execute` records the payment through the real path; a `SimulatedCrashError` is caught (`continue` skips to the next position, and the id is noted), and `finally` always disarms the flag. A recorded payment is counted, and when breaks are planted `_plant_after_capture` applies the position's break. After the loop it asks the provider for the day's rows and writes them with `format_csv`. The result carries the counts and the elapsed time.

```python
    def _maybe_crash(self, result: CaptureResult) -> None:
        if self._crash_armed:
            raise SimulatedCrashError(f"the process died after the provider captured {result.provider_txn_id}")
```

The hook given to `RecordCapture`: when armed, raises `SimulatedCrashError` after the provider has captured, leaving money moved at the provider and nothing in the ledger.

```python
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
```

After a payment is recorded, applies the break planted at its position. A chain of `if`/`elif` checks the position: the fee position makes the provider settle the other fee; the amount-mismatch position makes it settle the other amount; the refund position makes the provider refund the full amount an hour later without telling the service; the duplicate position records the same transaction again three seconds later. Lists passed in are filled with the affected ids.

```python
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
```

A retry with a brand-new key records the same provider transaction a second time: it reads the original payment of that transaction and calls `record_known_capture` with the source `SERVICE_RETRY` and a random key. This is the duplicate on our side.

```python
    def _time_of(self, plan: DayPlan, position: int, rng: random.Random) -> datetime:
        if plan.plant_breaks and position == plan.plants.timing:
            return datetime.combine(plan.business_date, plan.plants.late_capture_time, tzinfo=UTC)
        start, end = 8 * 3600, 22 * 3600 + 30 * 60  # 08:00 to 22:30, before the cutoff
        step = (end - start) / max(plan.payments, 1)
        seconds = int(start + (position - 1) * step + rng.uniform(0, step * 0.9))
        return datetime.combine(plan.business_date, time(0, 0), tzinfo=UTC) + timedelta(seconds=seconds)
```

The time of a payment. The planted timing break is placed at 23:40 UTC, after the 23:00 cutoff. Others are spread evenly between 08:00 and 22:30 (`start` and `end` are seconds since midnight): `step` is the spacing, and a random part up to 90 percent of a step is added (`rng.uniform`), then the seconds are added to midnight of the day.

```python
    @staticmethod
    def _amount_of(plan: DayPlan, position: int, rng: random.Random) -> int:
        drawn = rng.randint(500, 20_000)  # always drawn, so the stream does not depend on which positions are fixed
        if plan.plant_breaks:
            planted = plan.plants.amounts().get(position)
            if planted is not None:
                return planted
        return FIXED_AMOUNTS.get(position, drawn)
```

The amount of a payment. A random amount between 5.00 and 200.00 is **always** drawn, so the stream of random numbers does not depend on which positions are fixed. Planted positions return their planted amount; positions 1 and 2 return the fixed examples; the rest use the drawn amount. `@staticmethod` (above) means it does not use `self`.

### `src/payments_reconciliation/infrastructure/postgres.py` (472 lines)

```python
"""PostgreSQL: the connection pool, the unit of work, and the repositories behind the ports."""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from payments_reconciliation.application.ports import (
    BreakStatus,
    IdSource,
    LedgerRow,
    RefundRecord,
    RunRecord,
    StoredBreak,
    UnitOfWork,
)
from payments_reconciliation.domain.accounts import Account
from payments_reconciliation.domain.ledger import JournalEntry, Leg
from payments_reconciliation.domain.money import Money
from payments_reconciliation.domain.payment import LedgerPayment, PaymentSource, PaymentState
from payments_reconciliation.domain.reconciliation import Break, Cause
from payments_reconciliation.domain.settlement import SettlementKind
from payments_reconciliation.infrastructure.settings import PostgresSettings
```

All the PostgreSQL code: the connection pool, the unit of work and the repositories behind the ports. `hashlib` makes the ledger digest; `contextmanager` turns a generator function into something usable in a `with` block; `psycopg` is the database driver; `make_conninfo` builds the connection text; `ConnectionPool` keeps open connections ready. The long import from `application.ports` names the types the repositories implement.

```python
_PAYMENT = "id, provider_txn_id, currency, amount_minor, fee_minor, state, source, refunded_minor, captured_at"
_BREAK = (
    "id, run_date, provider_txn_id, kind, cause, status, currency, ledger_amount_minor, settlement_amount_minor, "
    "ledger_fee_minor, settlement_fee_minor, detail, note, cleared_on, updated_at"
)
```

Two constants with column lists, written once so the `SELECT` statements and the code that reads rows stay in step: `_PAYMENT` for the `payments` table and `_BREAK` for `reconciliation_breaks`. They contain no user input, so building SQL text with them is safe.

```python
def conninfo(settings: PostgresSettings) -> str:
    """The libpq connection string for `settings`; sessions run in UTC so dates are UTC calendar days."""
    return make_conninfo(
        host=settings.host,
        port=settings.port,
        dbname=settings.database,
        user=settings.user,
        password=settings.password,
        connect_timeout=settings.connect_timeout_seconds,
        application_name="payments-reconciliation",
        options="-c timezone=UTC",
    )
```

`conninfo` returns the connection string for `libpq` (PostgreSQL's client library): host, port, database name, user, password, a connect timeout, `application_name="payments-reconciliation"` (which makes this program's connections recognizable in the server's list of sessions) and `options="-c timezone=UTC"`, which makes every session run in UTC so that calendar days are UTC days.

```python
def open_pool(settings: PostgresSettings) -> ConnectionPool[psycopg.Connection[Any]]:
    """Open a pool and wait until it holds a working connection, or fail within the connect timeout."""
    pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(
        conninfo(settings), min_size=1, max_size=settings.pool_max_size, open=False, timeout=10.0
    )
    pool.open(wait=True, timeout=float(settings.connect_timeout_seconds) * 6)
    return pool
```

`open_pool` creates a pool without opening it (`open=False`), with at least one and at most `pool_max_size` connections and a 10-second wait for a free connection, then `pool.open(wait=True, timeout=...)` starts it and **waits** until a working connection exists or the timeout passes. A missing database becomes a loud error at startup.

```python
def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=UTC)
    return start, start + timedelta(days=1)
```

The start (midnight UTC) and end (24 hours later) of a calendar day, used for half-open queries `>= start AND < end`.

```python
class PostgresPayments:
    """`PaymentRepository` over the `payments` table."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The repository for the `payments` table. It receives one open connection.

```python
    def add(self, payment: LedgerPayment, idempotency_key: str | None = None) -> None:
        """Insert a payment."""
        self._connection.execute(
            "INSERT INTO payments (id, provider_txn_id, currency, amount_minor, fee_minor, state, source, "
            "refunded_minor, idempotency_key, captured_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                payment.id,
                payment.provider_txn_id,
                payment.amount.currency,
                payment.amount.minor,
                payment.fee.minor,
                payment.state.value,
                payment.source.value,
                payment.refunded.minor,
                idempotency_key,
                payment.captured_at,
            ),
        )
```

`add` inserts a payment. `%s` marks each value to be filled in; the values are passed separately in a tuple, so a value can never be read as SQL. Adjacent strings in parentheses are joined into one SQL text. The idempotency key column is `NULL` for payments that did not come through the capture path.

```python
    def by_key(self, idempotency_key: str) -> LedgerPayment | None:
        """The payment recorded for a capture request key, if any."""
        rows = self._query(f"SELECT {_PAYMENT} FROM payments WHERE idempotency_key = %s", (idempotency_key,))
        return rows[0] if rows else None
```

Reads the payment recorded for a key; `rows[0] if rows else None` returns the first row or `None`. The `f` before the string inserts `_PAYMENT` (trusted text); the key itself still goes through `%s`.

```python
    def lock_txn(self, provider_txn_id: str) -> None:
        """Serialize resolvers of one provider transaction until the transaction ends."""
        self._connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (provider_txn_id,))
```

Takes a transaction-scoped **advisory lock**: `pg_advisory_xact_lock(hashtextextended(%s, 1))`. `hashtextextended` turns the id text into a 64-bit number (a PostgreSQL internal function); the lock on that number is held until the transaction ends, so two resolvers of the same provider transaction take turns. Different transactions do not block each other.

```python
    def for_txn(self, provider_txn_id: str) -> list[LedgerPayment]:
        """Every payment carrying this provider transaction id, oldest first."""
        return self._query(
            f"SELECT {_PAYMENT} FROM payments WHERE provider_txn_id = %s ORDER BY captured_at, id", (provider_txn_id,)
        )
```

Every payment with this provider id, oldest first (`ORDER BY captured_at, id`).

```python
    def captured_on(self, day: date) -> list[LedgerPayment]:
        """Payments captured on a UTC calendar day, any state."""
        start, end = _day_bounds(day)
        return self._query(
            f"SELECT {_PAYMENT} FROM payments WHERE captured_at >= %s AND captured_at < %s "
            "ORDER BY provider_txn_id, id",
            (start, end),
        )
```

Payments captured on a UTC calendar day: `captured_at >= start AND captured_at < end`, in any state.

```python
    def by_txns(self, provider_txn_ids: Collection[str]) -> list[LedgerPayment]:
        """Payments for a set of provider transaction ids."""
        if not provider_txn_ids:
            return []
        return self._query(
            f"SELECT {_PAYMENT} FROM payments WHERE provider_txn_id = ANY(%s) "
            "ORDER BY provider_txn_id, captured_at, id",
            (list(provider_txn_ids),),
        )
```

Payments for a set of provider ids. An empty set returns `[]` without asking. `provider_txn_id = ANY(%s)` with a Python list sends the list as a PostgreSQL array.

```python
    def save(self, payment: LedgerPayment) -> None:
        """Write the changed state, fee and refunded total."""
        self._connection.execute(
            "UPDATE payments SET state = %s, fee_minor = %s, refunded_minor = %s WHERE id = %s",
            (payment.state.value, payment.fee.minor, payment.refunded.minor, payment.id),
        )
```

Updates the three columns that may change: state, fee and refunded total.

```python
    def _query(self, sql: str, params: Sequence[Any]) -> list[LedgerPayment]:
        rows = self._connection.execute(sql, params).fetchall()
        return [
            LedgerPayment(
                id=pid,
                provider_txn_id=txn,
                amount=Money(amount, currency.strip()),
                fee=Money(fee, currency.strip()),
                state=PaymentState(state),
                source=PaymentSource(source),
                captured_at=captured_at,
                refunded=Money(refunded, currency.strip()),
            )
            for pid, txn, currency, amount, fee, state, source, refunded, captured_at in rows
        ]
```

Runs a query and rebuilds `LedgerPayment` objects with a list comprehension that unpacks each row into named variables. `currency.strip()` removes padding because the column is `CHAR(3)`; the state and source texts become enum members.

```python
class PostgresLedger:
    """`LedgerRepository` over `journal_entries` and `postings`."""

    def __init__(self, connection: psycopg.Connection[Any], ids: IdSource) -> None:
        self._connection = connection
        self._ids = ids
```

The ledger repository over `journal_entries` and `postings`. It also receives the id source for entry ids.

```python
    def post(
        self, payment_id: UUID, operation: str, reference: str, entries: Sequence[JournalEntry], at: datetime
    ) -> None:
        """Append whole entries; the unique key on (payment, operation, reference, leg) refuses a repeat.

        The inserts go out in one pipeline, so a capture's nine statements cost one round trip.
        """
        with self._connection.pipeline():
            for entry in entries:
                entry_id = self._ids.new_id()
                self._connection.execute(
                    "INSERT INTO journal_entries (id, payment_id, operation, reference, leg, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (entry_id, payment_id, operation, reference, entry.leg.value, at),
                )
                for posting in entry.postings():
                    self._connection.execute(
                        "INSERT INTO postings (entry_id, payment_id, account, amount_minor, currency, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            entry_id,
                            payment_id,
                            posting.account.value,
                            posting.amount.minor,
                            posting.amount.currency,
                            at,
                        ),
                    )
```

Appends whole entries. `with self._connection.pipeline():` sends the statements in one batch without waiting for each answer (**pipeline mode**), so a capture's nine inserts cost one round trip. For each entry it inserts a `journal_entries` row, then that entry's two `postings` rows. The unique key on (payment, operation, reference, leg) makes the database refuse a second posting of the same leg.

```python
    def rows_for(self, payment_id: UUID) -> list[LedgerRow]:
        """Every posting of one payment, in the order posted."""
        rows = self._connection.execute(
            "SELECT e.leg, p.account, p.amount_minor, p.currency FROM postings p "
            "JOIN journal_entries e ON e.id = p.entry_id WHERE p.payment_id = %s ORDER BY p.id",
            (payment_id,),
        ).fetchall()
        return [
            LedgerRow(Leg(leg), Account(account), Money(minor, currency.strip()))
            for leg, account, minor, currency in rows
        ]
```

Joins postings to their entries to read a payment's rows in posting order (`ORDER BY p.id`) and rebuilds `LedgerRow` objects.

```python
    def total(self, currency: str) -> Money:
        """The sum of every posting in `currency`."""
        row = self._connection.execute(
            "SELECT COALESCE(sum(amount_minor), 0)::bigint FROM postings WHERE currency = %s", (currency,)
        ).fetchone()
        assert row is not None
        return Money(row[0], currency)
```

The sum of every posting in a currency. `COALESCE(sum(...), 0)` gives 0 for an empty table and `::bigint` casts the sum (PostgreSQL's `sum` of integers is a `NUMERIC`) back to an integer. `assert row is not None` tells the type checker the query always returns a row.

```python
    def digest(self) -> str:
        """A SHA-256 over every posting in insertion order."""
        digest = hashlib.sha256()
        cursor = self._connection.execute(
            "SELECT payment_id, account, amount_minor, currency FROM postings ORDER BY id"
        )
        for payment_id, account, amount, currency in cursor:
            digest.update(f"{payment_id}|{account}|{amount}|{currency}\n".encode())
        return digest.hexdigest()
```

A SHA-256 over every posting in insertion order. `hashlib.sha256()` starts a hash, `update` feeds it bytes (`.encode()` turns the text into bytes), and `hexdigest()` returns the result as hexadecimal text. A second resolve pass that leaves the digest unchanged proves it changed nothing.

```python
class PostgresRefunds:
    """`RefundRepository` over `refunds`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def add(self, refund: RefundRecord) -> None:
        """Insert a refund."""
        self._connection.execute(
            "INSERT INTO refunds (id, payment_id, amount_minor, currency, reference, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                refund.refund_id,
                refund.payment_id,
                refund.amount.minor,
                refund.amount.currency,
                refund.reference,
                refund.created_at,
            ),
        )

    def exists(self, payment_id: UUID, reference: str) -> bool:
        """True when this payment already has a refund with this reference."""
        row = self._connection.execute(
            "SELECT 1 FROM refunds WHERE payment_id = %s AND reference = %s", (payment_id, reference)
        ).fetchone()
        return row is not None

    def recorded_on(self, day: date, currency: str) -> dict[str, Money]:
        """Refunded totals by provider transaction id for refunds recorded on a UTC calendar day."""
        start, end = _day_bounds(day)
        rows = self._connection.execute(
            "SELECT p.provider_txn_id, sum(r.amount_minor)::bigint FROM refunds r "
            "JOIN payments p ON p.id = r.payment_id "
            "WHERE r.created_at >= %s AND r.created_at < %s AND r.currency = %s GROUP BY p.provider_txn_id",
            (start, end, currency),
        ).fetchall()
        return {txn: Money(total, currency) for txn, total in rows}
```

The refunds repository over `refunds`: `add` inserts a row; `exists` asks whether a refund with this payment and reference is there (`SELECT 1 ... fetchone()` returns a row or `None`); `recorded_on` sums the refunds created on a UTC day per provider transaction id (`GROUP BY`) into a dictionary of `Money`.

```python
class PostgresBreaks:
    """`BreakRepository` over `reconciliation_breaks`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The breaks repository over `reconciliation_breaks`.

```python
    def find(self, run_date: date, provider_txn_id: str, kind: SettlementKind, cause: str) -> StoredBreak | None:
        """The stored break with this natural key, if any."""
        rows = self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE run_date = %s AND provider_txn_id = %s "
            "AND kind = %s AND cause = %s",
            (run_date, provider_txn_id, kind.value, cause),
        )
        return rows[0] if rows else None
```

Finds a break by its natural key: run date, provider id, kind and cause.

```python
    def insert(self, run_date: date, finding: Break, status: BreakStatus, note: str, at: datetime) -> StoredBreak:
        """Store a newly found break."""
        currency = (finding.ledger_amount or finding.settlement_amount or Money.zero("USD")).currency
        row = self._connection.execute(
            "INSERT INTO reconciliation_breaks (run_date, provider_txn_id, kind, cause, status, currency, "
            "ledger_amount_minor, settlement_amount_minor, ledger_fee_minor, settlement_fee_minor, detail, note, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                run_date,
                finding.provider_txn_id,
                finding.kind.value,
                finding.cause.value,
                status.value,
                currency,
                _minor(finding.ledger_amount),
                _minor(finding.settlement_amount),
                _minor(finding.ledger_fee),
                _minor(finding.settlement_fee),
                finding.detail,
                note,
                at,
                at,
            ),
        ).fetchone()
        assert row is not None
        return StoredBreak(row[0], run_date, finding, status, note, at)
```

Stores a new break and returns it. The currency is taken from whichever side has an amount (`a or b or c` picks the first that is not empty). `RETURNING id` gives back the generated id in the same statement.

```python
    def get_for_update(self, break_id: int) -> StoredBreak | None:
        """Read and lock one break until the transaction ends."""
        rows = self._read(f"SELECT {_BREAK} FROM reconciliation_breaks WHERE id = %s FOR UPDATE", (break_id,))
        return rows[0] if rows else None
```

`FOR UPDATE` locks the row until the transaction ends.

```python
    def update(
        self, break_id: int, status: BreakStatus, note: str, at: datetime, cleared_on: date | None = None
    ) -> None:
        """Change a break's status and note."""
        self._connection.execute(
            "UPDATE reconciliation_breaks SET status = %s, note = %s, updated_at = %s, cleared_on = %s WHERE id = %s",
            (status.value, note, at, cleared_on, break_id),
        )
```

Changes the status, note, update time and, for a cleared break, the clearing date.

```python
    def refresh(self, break_id: int, finding: Break, at: datetime) -> None:
        """Replace the numbers of an unresolved break with this run's findings."""
        self._connection.execute(
            "UPDATE reconciliation_breaks SET ledger_amount_minor = %s, settlement_amount_minor = %s, "
            "ledger_fee_minor = %s, settlement_fee_minor = %s, detail = %s, updated_at = %s WHERE id = %s",
            (
                _minor(finding.ledger_amount),
                _minor(finding.settlement_amount),
                _minor(finding.ledger_fee),
                _minor(finding.settlement_fee),
                finding.detail,
                at,
                break_id,
            ),
        )
```

Replaces the amounts and detail of an unresolved break with this run's findings.

```python
    def for_run(self, run_date: date) -> list[StoredBreak]:
        """Every break of one run, in provider-id order."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE run_date = %s ORDER BY provider_txn_id, kind, cause",
            (run_date,),
        )
```

Every break of one run, in provider-id order.

```python
    def carried(self) -> list[StoredBreak]:
        """Timing differences still waiting, oldest first."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE status = 'CARRIED' ORDER BY run_date, provider_txn_id",
            (),
        )
```

Breaks still `CARRIED`, oldest first.

```python
    def cleared_on(self, day: date) -> list[StoredBreak]:
        """Carried breaks that cleared on this day's run."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE cleared_on = %s ORDER BY provider_txn_id", (day,)
        )
```

Breaks whose `cleared_on` is this day.

```python
    def worklist(self) -> list[StoredBreak]:
        """Breaks that need a person, oldest first."""
        return self._read(
            f"SELECT {_BREAK} FROM reconciliation_breaks WHERE status = 'NEEDS_REVIEW' "
            "ORDER BY run_date, provider_txn_id, kind",
            (),
        )
```

Breaks with status `NEEDS_REVIEW`, oldest first.

```python
    def _read(self, sql: str, params: Sequence[Any]) -> list[StoredBreak]:
        rows = self._connection.execute(sql, params).fetchall()
        result = []
        for (bid, run_date, txn, kind, cause, status, currency, ledger, settled, ledger_fee, settled_fee, detail, note,
             cleared_on, updated_at) in rows:  # fmt: skip
            currency = currency.strip()
            finding = Break(
                txn,
                SettlementKind(kind),
                Cause(cause),
                _money(ledger, currency),
                _money(settled, currency),
                _money(ledger_fee, currency),
                _money(settled_fee, currency),
                detail,
            )
            result.append(StoredBreak(bid, run_date, finding, BreakStatus(status), note, updated_at, cleared_on))
        return result
```

Runs a query and rebuilds `StoredBreak` objects. Each row is unpacked into named variables (the `# fmt: skip` comment tells the code formatter to leave this layout alone), the currency is stripped, and the amounts become `Money` or `None` through `_money`.

```python
class PostgresRuns:
    """`RunRepository` over `reconciliation_runs`."""

    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection
```

The runs repository over `reconciliation_runs`.

```python
    def save(self, run: RunRecord) -> None:
        """Record the first pass of a day; a later run of the same day only counts itself."""
        self._connection.execute(
            "INSERT INTO reconciliation_runs (run_date, currency, ledger_payments, settlement_rows, "
            "settled_total_minor, refunded_total_minor, clean_transactions, clean_total_minor, break_count, ran_at, "
            "last_ran_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (run_date) DO UPDATE SET "
            "reruns = reconciliation_runs.reruns + 1, last_ran_at = excluded.last_ran_at",
            (
                run.run_date,
                run.currency,
                run.ledger_payments,
                run.settlement_rows,
                run.settled_total.minor,
                run.refunded_total.minor,
                run.clean_transactions,
                run.clean_total.minor,
                run.break_count,
                run.ran_at,
                run.ran_at,
            ),
        )
```

Records the first pass of a day. `ON CONFLICT (run_date) DO UPDATE SET reruns = reconciliation_runs.reruns + 1, last_ran_at = excluded.last_ran_at` means: if the day already has a run, do not overwrite the first-pass numbers; only count the re-run and note when it happened (`excluded` is the row that was rejected).

```python
    def get(self, run_date: date) -> RunRecord | None:
        """The run for a date, if it ran."""
        row = self._connection.execute(
            "SELECT currency, ledger_payments, settlement_rows, settled_total_minor, refunded_total_minor, "
            "clean_transactions, clean_total_minor, break_count, ran_at FROM reconciliation_runs WHERE run_date = %s",
            (run_date,),
        ).fetchone()
        if row is None:
            return None
        currency, payments, rows, settled, refunded, clean, clean_total, breaks, ran_at = row
        currency = currency.strip()
        return RunRecord(
            run_date, currency, payments, rows, Money(settled, currency), Money(refunded, currency), clean,
            Money(clean_total, currency), breaks, ran_at,
        )  # fmt: skip
```

Reads the run of a date and rebuilds the `RunRecord`, or returns `None`.

```python
def _minor(amount: Money | None) -> int | None:
    return None if amount is None else amount.minor
```

Two small converters between `Money | None` and the integer or `NULL` stored in the database.

```python
def _money(minor: int | None, currency: str) -> Money | None:
    return None if minor is None else Money(minor, currency)
```

The other direction.

```python
@dataclass(slots=True)
class PostgresUnitOfWork:
    """The repositories of one connection, all inside the transaction the pool opened for it."""

    payments: PostgresPayments
    ledger: PostgresLedger
    refunds: PostgresRefunds
    breaks: PostgresBreaks
    runs: PostgresRuns
```

A plain bundle of the five repositories for one connection.

```python
class PostgresUnitOfWorkFactory:
    """Open a transaction on a pooled connection: commit on a normal exit, roll back on an exception."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]], ids: IdSource) -> None:
        self._pool = pool
        self._ids = ids

    @contextmanager
    def __call__(self) -> Iterator[UnitOfWork]:
        """One unit of work."""
        with self._pool.connection() as connection:
            yield PostgresUnitOfWork(
                PostgresPayments(connection),
                PostgresLedger(connection, self._ids),
                PostgresRefunds(connection),
                PostgresBreaks(connection),
                PostgresRuns(connection),
            )
```

Opens a unit of work on a pooled connection. `@contextmanager` on `__call__` turns the method into a `with`-block provider: `with self._pool.connection() as connection:` borrows a connection and starts a transaction; the generator `yield`s a `PostgresUnitOfWork` for the block's body; when the body ends normally the pool commits, when it raises the pool rolls back, and the connection goes back to the pool.

```python
class PostgresHealth:
    """`HealthProbe`: can a pooled connection run `SELECT 1`?"""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[Any]]) -> None:
        self._pool = pool

    def check(self) -> None:
        """Return normally when healthy; raise when not."""
        with self._pool.connection(timeout=2.0) as connection:
            connection.execute("SELECT 1")
```

The health probe: borrow a connection (waiting at most 2 seconds) and run `SELECT 1`. If anything raises, the health check reports the failure.

## Try it yourself

1. Run the unit tests of these adapters (no database needed):

   ```bash
   .venv/bin/python -m pytest tests/unit/infrastructure -q
   ```

   Expected: all pass.

2. Read the settings the program would use: `POSTGRES_PASSWORD=x .venv/bin/python -c "from payments_reconciliation.infrastructure.settings import Settings; print(Settings.from_env())"`. Expected: a `Settings(...)` line without the password.

3. Simulate a day and look at the provider's file (needs `make up`):

   ```bash
   make up
   .venv/bin/payments-reconciliation simulate-day --date 2026-10-20 --payments 10 --plant
   head -5 var/settlement/settlement_2026-10-20.csv
   ```

   Expected: a summary line and a CSV that starts `provider_txn_id,amount,fee,status,settled_at`.

## Mistakes people make

- **Reading a password with a default.** The password has none; a missing one stops the program.
- **Writing a file in place.** A reader could see half of it; the adapter writes to a temporary name and renames.
- **Mixing time zones.** Sessions and all times are UTC so a calendar day means the same thing everywhere.
- **Building SQL by joining user input.** Values always go through `%s` parameters; only fixed column lists are joined into the text.

## Check yourself

1. Why is the settlement file written to a `.part` name first? *So a reader never sees a half-written file; the rename is atomic.*
2. What does `lock_txn` guarantee? *Resolvers of one provider transaction take turns until the transaction ends.*
3. What makes a simulated day repeatable? *A seeded random generator and sequential provider ids.*
4. What does `ON CONFLICT ... DO UPDATE` do in `PostgresRuns.save`? *Keeps the first-pass numbers and only counts the re-run.*
5. Why does `post` use a pipeline? *To send a capture's inserts in one round trip.*

## Further reading

- psycopg 3 documentation: https://www.psycopg.org/psycopg3/docs/
- psycopg_pool: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
- PostgreSQL advisory locks: https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS
- Python 3.12 `random`: https://docs.python.org/3.12/library/random.html
- Python 3.12 `os.replace`: https://docs.python.org/3.12/library/os.html#os.replace
