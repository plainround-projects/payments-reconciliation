# psycopg and psycopg_pool

## What it is

`psycopg` (version 3) is the Python **driver** for PostgreSQL: a library that opens a network connection to the database, sends it SQL text and reads back rows. `psycopg_pool` is its companion package that keeps a **pool** of open connections ready, so each request does not pay the cost of connecting. The `[binary]` extra installs a prebuilt C library (`psycopg-binary`) so nothing needs compiling.

## Why this repository uses it

All state lives in PostgreSQL: payments, ledger rows, refunds, and the reconciliation runs and breaks. A resolver must write a ledger correction and update the break's status **in one transaction**, and a second resolver must not run at the same moment on the same transaction id. psycopg gives explicit transactions, row locks and advisory locks, and its pool serves the HTTP API's concurrent requests.

## Where it appears

| Path | Role |
| --- | --- |
| `src/payments_reconciliation/infrastructure/postgres.py` | All database code (472 lines): connection string, pool, the repositories (payments, ledger, refunds, breaks, runs), the unit of work, the health probe. |
| `src/payments_reconciliation/infrastructure/settings.py` | The values for the connection (`PostgresSettings`). |
| `tests/conftest.py`, `tests/integration/*` | Open plain connections to load the schema and to test it. |
| `requirements.lock` | `psycopg==3.3.6`, `psycopg-binary==3.3.6`, `psycopg-pool==3.3.3`. |

## How it works underneath

1. `psycopg.connect(...)` opens a TCP connection to the server (host and port) and logs in with a user and password. The wire protocol is PostgreSQL's own.
2. By default a connection starts a **transaction** the first time you run a statement; nothing is permanent until `commit()`. `rollback()` throws the changes away.
3. `conn.execute(sql, params)` sends the SQL text and the parameters **separately**. The server fills the placeholders `%s` itself, so a parameter can never turn into SQL (the defense against SQL injection).
4. Results come back as rows; `fetchone()` gives one row (a tuple) or `None`, `fetchall()` gives a list, and iterating the cursor streams rows one at a time.
5. The pool opens up to `max_size` connections, lends one to `pool.connection()`, and takes it back afterwards. If all are lent, the caller waits up to a `timeout`.
6. In **pipeline mode** the client sends several statements without waiting for each answer, and reads the answers afterwards, saving network round trips.

## Walkthrough of this repository's use

### The connection string

```python
return make_conninfo(
    host=settings.host, port=settings.port, dbname=settings.database,
    user=settings.user, password=settings.password,
    connect_timeout=settings.connect_timeout_seconds,
    application_name="payments-reconciliation",
    options="-c timezone=UTC",
)
```

`make_conninfo` builds the text PostgreSQL's client library (**libpq**) understands, quoting values correctly. `application_name` shows up in the server's `pg_stat_activity`. `options="-c timezone=UTC"` sets the session time zone for every connection, so "the day of a timestamp" is always a UTC calendar day.

### Opening the pool

```python
pool = ConnectionPool(conninfo(settings), min_size=1, max_size=settings.pool_max_size, open=False, timeout=10.0)
pool.open(wait=True, timeout=float(settings.connect_timeout_seconds) * 6)
```

`min_size=1`: keep at least one connection. `max_size`: from `DB_POOL_MAX_SIZE`, default 10. `open=False` creates the pool object without starting its background workers; `pool.open(wait=True, timeout=...)` then starts them and **waits** until a working connection exists, or fails after 30 seconds (six times the connect timeout, which gives a database that is still starting some time). A missing database becomes a loud startup error instead of a hang on the first request. `timeout=10.0` is how long a request waits for a free connection.

### One unit of work = one transaction

```python
@contextmanager
def __call__(self) -> Iterator[UnitOfWork]:
    with self._pool.connection() as connection:
        yield PostgresUnitOfWork(PostgresPayments(connection), PostgresLedger(connection, self._ids), ...)
```

The documented behavior of `pool.connection()`: leaving the block normally **commits** the open transaction; leaving it with an exception **rolls it back**; then the connection returns to the pool. So everything done through the repositories inside one `with` is all-or-nothing. Reconciling a day, and resolving **one break**, each run in their own unit of work: a correction and the status change of its break commit together or not at all.

### Parameters and placeholders

```python
self._connection.execute(
    "UPDATE payments SET state = %s, fee_minor = %s, refunded_minor = %s WHERE id = %s",
    (payment.state.value, payment.fee.minor, payment.refunded.minor, payment.id),
)
```

`%s` marks a value to fill; the tuple holds them in order. Python values are converted for you: `UUID` to PostgreSQL `uuid`, `datetime` to `timestamptz`, `date` to `date`, `None` to `NULL`. A Python **list** becomes a PostgreSQL array, which is used for the set of carried transaction ids:

```python
f"SELECT {_PAYMENT} FROM payments WHERE provider_txn_id = ANY(%s) ORDER BY provider_txn_id, captured_at, id",
(list(provider_txn_ids),),
```

The one place SQL text is built with an f-string is the fixed column lists (`_PAYMENT`, `_BREAK`), constants in the source, never user input. The empty case is handled before the query (`if not provider_txn_ids: return []`).

### Getting an id back

```python
row = self._connection.execute("INSERT INTO reconciliation_breaks (...) VALUES (...) RETURNING id", (...)).fetchone()
assert row is not None
return StoredBreak(row[0], ...)
```

`RETURNING id` makes the insert also return the number the database assigned. `fetchone()` returns a tuple, so `row[0]` is the id.

### Locks

```python
"SELECT ... FROM reconciliation_breaks WHERE id = %s FOR UPDATE"
"SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))"
```

`FOR UPDATE` locks the row until the transaction ends, so two resolvers on one break take turns and the second sees `RESOLVED`. The **advisory lock** is taken on the provider transaction id (`hashtextextended` turns text into a 64-bit number, the `1` is a seed that separates this use from other uses): resolvers of different breaks on the same transaction also wait for each other. Both release automatically at the end of the transaction.

### Pipeline mode

```python
with self._connection.pipeline():
    for entry in entries:
        self._connection.execute("INSERT INTO journal_entries ...", (...))
        for posting in entry.postings():
            self._connection.execute("INSERT INTO postings ...", (...))
```

A capture is nine insert statements. Inside `with connection.pipeline():` they are sent together and the results read at the end of the block, so they cost about one network round trip instead of nine. The docstring says so. The rules (the deferred balance trigger) still run at `COMMIT`.

### Streaming a hash of the ledger

```python
cursor = self._connection.execute("SELECT payment_id, account, amount_minor, currency FROM postings ORDER BY id")
for payment_id, account, amount, currency in cursor:
    digest.update(f"{payment_id}|{account}|{amount}|{currency}\n".encode())
```

Iterating a cursor yields one row at a time. The program uses this to compute a SHA-256 "digest" of every posting in insertion order; the demo prints the digest before and after running the resolvers a second time to show that **nothing changed**.

### Health probe

```python
with self._pool.connection(timeout=2.0) as connection:
    connection.execute("SELECT 1")
```

Borrow a connection (waiting at most 2 seconds) and run the smallest query. If it raises, `/health` answers 503.

### In the tests

`errors.CheckViolation` (from `psycopg.errors`) is what a failed `CHECK` constraint raises; `pytest.raises(errors.CheckViolation)` proves the database refuses unbalanced entries. `wait_until_ready` in `tests/conftest.py` retries `psycopg.connect` while the container is starting, catching `psycopg.OperationalError`.

## Try it yourself

1. With the database up (`make up`) and the API running (`make serve`), see the pooled connections:

   ```bash
   docker compose exec -T postgres psql -U reconciliation -d reconciliation -c "select application_name, state from pg_stat_activity where application_name = 'payments-reconciliation'"
   ```

   Expected: one or more rows named `payments-reconciliation` (none if the server is not running).

2. Run one query from Python:

   ```bash
   set -a; . ./.env; set +a; .venv/bin/python -c "
   import psycopg
   from payments_reconciliation.infrastructure import postgres
   from payments_reconciliation.infrastructure.settings import Settings
   s = Settings.from_env()
   with psycopg.connect(postgres.conninfo(s.postgres)) as c:
       print(c.execute('select count(*) from payments').fetchone(), c.execute('show timezone').fetchone())"
   ```

   Expected after `make db`: `(6,) ('UTC',)` (the six seeded payments, and the session zone set by the connection option). `set -a; . ./.env; set +a` loads the variables from `.env` into the shell.

3. Prove rollback: run `make test-integration` and read the `CheckViolation` the schema tests expect.

## Mistakes people make

- **Building SQL with f-strings and user values.** Always pass values as parameters.
- **Forgetting to commit.** Outside a `with` block the work is lost on close. The unit of work commits on a normal exit.
- **Holding a pooled connection too long.** The pool is limited; each unit of work here is short.
- **Using one connection from many threads.** Take one per unit of work from the pool.
- **Trusting `CHAR(3)` to be unpadded.** The code calls `.strip()` on currencies read back.
- **Passing an empty list to `= ANY(...)`** and expecting rows. It matches nothing; the code returns early.

## Check yourself

1. What does leaving `with pool.connection()` do after an exception? *Rolls the transaction back and returns the connection to the pool.*
2. Why are parameters sent separately from the SQL? *So values can never be interpreted as SQL.*
3. What does `FOR UPDATE` do? *Locks the selected rows until the transaction ends.*
4. What does pipeline mode save? *Network round trips: statements are sent together and the answers read later.*
5. What does `RETURNING id` do? *Returns the id the database assigned to the inserted row.*

## Further reading

- psycopg 3.3 documentation: https://www.psycopg.org/psycopg3/docs/
- Connection pools: https://www.psycopg.org/psycopg3/docs/advanced/pool.html
- Transactions: https://www.psycopg.org/psycopg3/docs/basic/transactions.html
- Pipeline mode: https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html
- Adapting types (lists as arrays, dates): https://www.psycopg.org/psycopg3/docs/basic/adapt.html
