# CSV

## What it is

CSV (comma-separated values) is a plain-text format for tables. Each line is a row; the values in a row are separated by commas; the first line usually names the columns. Spreadsheets, databases and payment providers all read and write it. Python's standard library has a `csv` module that reads and writes it correctly, including values that contain commas or quotes.

## Why this repository uses it

The provider's **settlement report** arrives as a CSV file, one per business day. It is the other half of the reconciliation: the provider's own list of what it settled and refunded. The program must read it strictly (a bad line is an error, never skipped) and the simulator must be able to write the same format, so tests and demos can produce a realistic file.

## Where it appears

| Path | What it does with CSV |
| --- | --- |
| `data/settlement_2026-10-19.csv` | A checked-in sample file: six settled rows for the seeded day. |
| `src/payments_reconciliation/domain/settlement.py` | `parse_csv()` reads the text into a `SettlementReport`; `format_csv()` writes rows as text. |
| `src/payments_reconciliation/infrastructure/files.py` | Reads and writes the files `settlement_YYYY-MM-DD.csv` in the settlement folder. |
| `src/payments_reconciliation/infrastructure/simulation.py` | Produces the day's file for the simulated provider. |
| `var/settlement/` | Where `make run` and `make reconcile-sample` put the files (generated; git-ignored). |
| `tests/unit/domain/test_settlement.py` | Tests of the parser and the writer. |

## How it works underneath

A CSV file is only text. The reader splits each line at commas, except inside double quotes, and gives back a list of strings for each row. Everything is a string: the reader does not know that `42.50` is an amount or that `2026-10-19T23:30:01Z` is a time. The program must convert, and must decide what to do with a line that has the wrong number of columns or a value that does not convert.

## Walkthrough of this repository's use

### The file

```
provider_txn_id,amount,fee,status,settled_at
ptx_s0001,42.50,1.23,settled,2026-10-19T23:30:01Z
ptx_s0002,18.00,0.60,settled,2026-10-19T23:30:02Z
```

Five columns: the provider's transaction id (the key that joins to the ledger), the amount and the fee in decimal text, a status (`settled` or `refunded`), and the time the provider settled it, in UTC (`Z` means UTC). A refund has the same transaction id as its capture and a **negative** amount.

### Reading it

```python
reader = csv.reader(io.StringIO(text))
try:
    header = next(reader)
except StopIteration as error:
    raise SettlementFormatError("the settlement file is empty") from error
if header != HEADER:
    raise SettlementFormatError(f"expected the header {','.join(HEADER)}, got {','.join(header)}")
```

- `io.StringIO(text)` makes a string behave like a file, so `csv.reader` can read it. The rest of the program passes the file's **text** around, not file objects, which keeps the parsing a pure function that tests can call with a literal string.
- `next(reader)` takes the first row. If the file is empty `next` raises `StopIteration`, which the code turns into a clear, named error.
- The header must match exactly. A report in a different layout is refused, not guessed at.

```python
for number, record in enumerate(reader, start=2):
    if not record:
        continue
    if len(record) != len(HEADER):
        raise SettlementFormatError(f"line {number}: expected {len(HEADER)} columns, got {len(record)}")
    txn_id, amount, fee, status, stamp = record
    ...
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
```

- `enumerate(reader, start=2)` numbers rows from 2, the line number in the file (line 1 is the header), so every error names its line.
- Blank lines are skipped; short or long lines are errors.
- `Money.parse(amount, currency)` converts the text exactly (no floats). `strptime` converts the time using a format string; `%Y-%m-%dT%H:%M:%SZ` means year-month-dayThour:minute:secondZ. `.replace(tzinfo=UTC)` marks it as UTC.
- A `ValueError` or domain error from any conversion is re-raised as `SettlementFormatError(f"line {number}: ...")`. **Nothing is skipped**: a half-read report would produce false breaks, so the job stops instead.

### Writing it

```python
buffer = io.StringIO()
writer = csv.writer(buffer, lineterminator="\n")
writer.writerow(HEADER)
for row in rows:
    stamp = row.settled_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    writer.writerow([row.provider_txn_id, row.amount.format(), row.fee.format(), row.kind.value, stamp])
return buffer.getvalue()
```

`csv.writer` quotes a value if it needs it. `lineterminator="\n"` forces Unix line endings (the writer's default is `\r\n`), so the file is the same on every system. `Money.format()` writes `42.50`, never `42.5`.

### One file per day, written safely

`DirectorySettlementFiles` names a file `settlement_{date}.csv`. `write` stores text in `settlement_....csv.part` and then renames it over the real name with `os.replace`, so a reader never sees a half-written file. `read` raises `SettlementMissingError` (answered with 404 by the HTTP API) when the provider's file has not arrived.

## Try it yourself

1. Look at the sample file: `cat data/settlement_2026-10-19.csv`. Expected: a header and six rows.

2. Read it with the program's own parser:

   ```bash
   .venv/bin/python -c "
   from datetime import date
   from payments_reconciliation.domain.settlement import parse_csv
   report = parse_csv(open('data/settlement_2026-10-19.csv').read(), date(2026, 10, 19), 'USD')
   print(len(report.rows), report.settled_total.format())"
   ```

   Expected: `6 200.49`.

3. Feed it a bad header:

   ```bash
   .venv/bin/python -c "
   from datetime import date
   from payments_reconciliation.domain.settlement import parse_csv
   parse_csv('a,b\n', date(2026, 10, 19), 'USD')" 2>&1 | tail -1
   ```

   Expected: `payments_reconciliation.domain.errors.SettlementFormatError: expected the header provider_txn_id,amount,fee,status,settled_at, got a,b`.

4. Reconcile the sample: `make reconcile-sample` (needs `make up`). Expected: `2026-10-19: 6 settlement rows, 6 payments in the ledger, 5 transactions matched cleanly`, and one break, `ptx_s0002  fee_discrepancy`.

## Mistakes people make

- **Treating values as numbers or dates automatically.** They are text until you convert them.
- **Parsing with `line.split(",")`.** It breaks on quoted values that contain commas; use `csv`.
- **Silently skipping bad lines.** That hides data loss; here a bad line stops the job and names its number.
- **Using floats for amounts** (`float("42.50")`). Use exact decimals or integers.
- **Forgetting the encoding and line endings.** Files are read and written as UTF-8 with `\n`.

## Check yourself

1. What type does `csv.reader` give for the value `42.50`? *A string; the program converts it.*
2. Why does the parser pass the file's text, not a file object? *It keeps parsing a pure function that tests can call with a string.*
3. What happens to a line with four columns instead of five? *`SettlementFormatError` naming the line number.*
4. Why does the writer use `lineterminator="\n"`? *To get Unix line endings regardless of the platform.*
5. How is a refund shown in the file? *A `refunded` row with the capture's transaction id and a negative amount.*

## Further reading

- Python `csv` (3.12): https://docs.python.org/3.12/library/csv.html
- `io.StringIO`: https://docs.python.org/3.12/library/io.html#io.StringIO
- `datetime.strptime` format codes: https://docs.python.org/3.12/library/datetime.html#strftime-and-strptime-format-codes
- RFC 4180, the CSV format: https://www.rfc-editor.org/rfc/rfc4180
