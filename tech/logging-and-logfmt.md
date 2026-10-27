# Python logging and the logfmt format

## What it is

**Logging** is a program writing a short line about what it is doing, so people can find out later what happened. Python's standard library has a `logging` module: code calls `logger.info("message")`, and **handlers** and **formatters** decide where the line goes and what it looks like. **logfmt** is a simple log line format of `key=value` pairs separated by spaces, easy for people and for tools to read.

## Why this repository uses it

A reconciliation job must be explainable after the fact ("what happened to ptx_0112?") without ever leaking card data. Structured logs give each event a message plus fields (`provider_txn_id`, `cause`, `outcome`, `status`, `duration_ms`). A formatter masks anything that looks like a card number and withholds named secret fields, as a second line of defense behind the rule "the code never logs card data".

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../infrastructure/logging.py` | `LogfmtFormatter`, `redact`, `configure_logging`. |
| `src/.../application/logs.py` | `fields(**values)`, the helper that attaches key-value pairs. |
| `src/.../application/*.py` | `logger.info("break handled", extra=fields(provider_txn_id=..., cause=..., outcome=...))`. |
| `src/.../interfaces/http/app.py` | The request-logging middleware. |
| `src/.../interfaces/serve.py` | Calls `configure_logging(settings.log_level)` at startup. |
| `tests/unit/infrastructure/test_settings_logging_retry.py` | Tests the masking. |

## How it works underneath

1. Each module gets a named logger: `logger = logging.getLogger(__name__)`. Names form a tree (`payments_reconciliation.application.resolve_breaks` is a child of `payments_reconciliation`).
2. Calling `logger.info(...)` creates a **record** (message, level, time, extra attributes) and passes it up to the **root logger**'s handlers, if the level is high enough.
3. A **handler** (here `StreamHandler`) writes the record to a stream (standard error). Its **formatter** turns the record into text.
4. Levels, from quiet to loud: `DEBUG`, `INFO`, `WARNING`, `ERROR`. The configured level is the minimum that gets through.

## Walkthrough of this repository's use

### Attaching fields

```python
def fields(**values: Any) -> dict[str, Any]:
    return {"fields": values}

logger.info(
    "break handled",
    extra=fields(provider_txn_id=stored.finding.provider_txn_id, cause=cause.value, outcome=outcome.value),
)
```

`extra={"fields": {...}}` puts a `fields` attribute on the record. The formatter reads it. The docstring warns: never pass card data, tokens or secrets.

### The formatter

```python
class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")
        parts = [f"ts={timestamp}", f"level={record.levelname}", f"logger={record.name}", f"msg={_quote(record.getMessage())}"]
        extra: dict[str, Any] = getattr(record, "fields", {})
        parts.extend(f"{key}={'[withheld]' if key in _SECRET_FIELDS else _quote(value)}" for key, value in extra.items())
        if record.exc_info:
            parts.append(f"exc={_quote(self.formatException(record.exc_info))}")
        return " ".join(parts)
```

- The first four pairs are always present: time in UTC to milliseconds, level, logger name, message.
- Each field becomes `key=value`. If the key is in `_SECRET_FIELDS` (`card_token`, `card_number`, `cvv`, `password`, `authorization`) the value is replaced by `[withheld]`, whatever it was.
- An exception adds `exc=...` with the traceback text.
- Lines are joined with single spaces.

### Quoting and masking

```python
_CARD_NUMBER = re.compile(r"(?<![\w.])(?:\d[ -]?){13,19}(?![\w])")

def redact(text: str) -> str:
    return _CARD_NUMBER.sub("[redacted-number]", text)

def _quote(value: object) -> str:
    text = redact(str(value))
    return json.dumps(text) if not text or any(char in text for char in ' ="\n') else text
```

- The regular expression finds 13 to 19 digits, optionally separated by single spaces or dashes, that are not part of a longer word or a decimal number: the shape of a card number. `(?<![\w.])` ("not preceded by a word character or a dot") and `(?![\w])` ("not followed by a word character") avoid matching inside identifiers and prices.
- `_quote` first masks, then wraps the value in JSON-style double quotes if it is empty or contains a space, `=`, `"` or a newline, so a line can be split back into pairs unambiguously.

### Configuring it

```python
def configure_logging(level: str, stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(LogfmtFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(max(logging.getLevelName(level), logging.WARNING))
```

One handler on the root logger, writing to standard error. `root.handlers[:] = [handler]` replaces any handlers, which makes the function safe to call twice. The two library loggers are held at warning or above because their routine messages are noise.

### What a line looks like

```
ts=2026-10-27T12:00:00.123+00:00 level=INFO logger=payments_reconciliation.application.resolve_breaks msg="break handled" provider_txn_id=ptx_0112 cause=fee_discrepancy outcome=resolved
```

## Try it yourself

1. Format a record by hand:

   ```bash
   .venv/bin/python -c "
   import logging, sys
   from payments_reconciliation.infrastructure.logging import configure_logging
   from payments_reconciliation.application.logs import fields
   configure_logging('INFO', sys.stdout)
   logging.getLogger('demo').info('hello world', extra=fields(card_token='tok_x', note='card 4111 1111 1111 1111 here', n=3))"
   ```

   Expected: one line such as `ts=... level=INFO logger=demo msg="hello world" card_token=[withheld] note="card [redacted-number] here" n=3`.

2. Run the logging tests: `.venv/bin/python -m pytest tests/unit/infrastructure/test_settings_logging_retry.py -q`.

3. Change verbosity: `LOG_LEVEL=DEBUG make serve` shows more; `LOG_LEVEL=WARNING` shows less.

4. Watch a request being logged: with the server running, send any request and look in the server terminal for `msg=request ... method=POST path=/v1/days/2026-10-20/reconcile status=200 duration_ms=...`.

## Mistakes people make

- **Logging request bodies or tokens.** The middleware logs method, path, status and time only.
- **Building the message with an f-string** that contains data (`f"charged {card}"`) and bypasses the fields mechanism.
- **Free-text logs.** Searching them needs fragile patterns; key=value fields can be filtered exactly.
- **Treating redaction as the only defense.** It is a backstop; the first rule is never to log such data. The card number used in the example above is a published test number.
- **Calling `basicConfig` in many places.** One `configure_logging` call at startup.

## Check yourself

1. What is the root logger? *The top of the logger tree; handlers attached to it receive all records.*
2. How does the formatter keep secrets out of logs? *Names in `_SECRET_FIELDS` are replaced with `[withheld]`, and card-number-shaped digit runs are replaced with `[redacted-number]`.*
3. Why quote values with spaces? *So a line can be split into pairs without ambiguity.*
4. What does `extra=fields(...)` do? *Attaches a `fields` dict to the log record for the formatter to print.*
5. Where do the logs go? *Standard error.*

## Further reading

- Python `logging` (3.12): https://docs.python.org/3.12/library/logging.html
- Logging HOWTO: https://docs.python.org/3.12/howto/logging.html
- `re` (regular expressions): https://docs.python.org/3.12/library/re.html
- The logfmt convention: https://brandur.org/logfmt
