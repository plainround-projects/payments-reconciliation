# PostgreSQL

## What it is

PostgreSQL (often "Postgres") is a **relational database server**: a long-running program that stores data in tables, answers queries written in SQL, and promises that a group of changes either all happens or none of it does. Clients (here the Python program and the `psql` command) connect to it over the network, by default on port 5432. This repository pins **major version 18** (`image: postgres:18`).

## Why this repository uses it

Two kinds of data live here. The **ledger** (payments, journal entries, postings, refunds) is the books being reconciled. The **reconciliation results** (one run per day, one row per break with its cause and status) are the job's own record. PostgreSQL enforces the rules on both: an unbalanced entry, an edit to a posted row and a second posting of the same operation are impossible whatever the Python code does, and a break can exist only once per day, transaction, kind and cause, so running a day twice cannot pile up duplicates.

## Where it appears

| Path | Role |
| --- | --- |
| `docker-compose.yml` | Runs the `postgres:18` container on host port 54612, with a health check. |
| `sql/*.sql` | The schema, seed data, queries and migrations (see `sql.md`). |
| `src/payments_reconciliation/infrastructure/postgres.py` | The only Python file that talks to it. |
| `Makefile` | `up`, `db`, `queries`, `migrate`, `down` drive it through `psql`. |
| `tests/integration/` | Tests that run against the real server. |
| `.github/workflows/ci.yml` | Starts `postgres:18` as a service container. |

## How it works underneath

- **Server process and data directory.** The container runs `postgres`, which keeps tables as files in its data directory and a **write-ahead log** (WAL): every change is first written to the log, then to the table files. After a crash, the log is replayed.
- **Transactions.** `BEGIN` ... `COMMIT` groups statements. Until `COMMIT`, other connections do not see the changes. `ROLLBACK` discards them. A failed statement inside a transaction aborts the whole transaction.
- **Constraints.** `NOT NULL`, `CHECK`, `UNIQUE`, `PRIMARY KEY` and `FOREIGN KEY` are tested by the server on every write. A violation raises an error and nothing is stored.
- **Triggers.** A trigger runs a function automatically on `INSERT`, `UPDATE` or `DELETE`. A **constraint trigger** can be *deferred*: it runs at `COMMIT` instead of after each statement.
- **Locks.** A row lock (`SELECT ... FOR UPDATE`) makes other writers of that row wait until your transaction ends. An **advisory lock** is a lock on a number your program picks; it has no link to any table.
- **Session time zone.** A `timestamptz` value is stored as an instant; the session's `timezone` setting decides how it is shown and what "today" means for `::date` conversions.

## Walkthrough of this repository's use

### The container

```yaml
postgres:
  image: postgres:18
  command: ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
  ports:
    - "${POSTGRES_PORT:-54612}:5432"
  environment:
    POSTGRES_USER: ${POSTGRES_USER:-reconciliation}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?copy .env.example to .env first}
    POSTGRES_DB: ${POSTGRES_DB:-reconciliation}
```

- The three `-c` flags change server settings. Normally PostgreSQL waits for the disk before saying a commit is done (`fsync`, `synchronous_commit`, `full_page_writes`). Switching them off makes tests fast on a busy machine, but the official documentation warns that `fsync=off` can corrupt the database after an operating-system or hardware crash, and `synchronous_commit=off` can lose the last few committed transactions. That is acceptable only because this database is **disposable** (rebuilt by `make db`). Never copy these flags to a database that holds data you need.
- `5432` is the port inside the container; `54612` is the port on your computer, chosen to avoid clashing with other projects.
- `POSTGRES_PASSWORD` comes from `.env`; the `:?message` form makes Compose stop with that message if it is unset, so no password is written in the file.

### Health

```yaml
test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
```

`pg_isready` asks "is the server accepting connections?" and exits 0 if yes. `-h 127.0.0.1` makes it use TCP: while the image first starts it runs a temporary server (for creating the database) that only listens on a local socket, and a socket-based check would report ready too early.

### Keeping the books honest (from `sql/001_schema.sql`)

```sql
CONSTRAINT payments_fee_bounds CHECK (fee_minor BETWEEN 0 AND amount_minor),
CONSTRAINT journal_entries_once_per_leg UNIQUE (payment_id, operation, reference, leg),
```

- The `UNIQUE` constraint makes it impossible to post the same leg of the same operation twice. A repeated resolver run hits this wall instead of double-posting.
- The comment on the `payments` table explains a deliberate choice: **`provider_txn_id` is not unique**. This is the ledger *under reconciliation*; a duplicate row for one provider transaction is a break to find, not a write to refuse.

```sql
CREATE CONSTRAINT TRIGGER postings_entry_balanced
    AFTER INSERT ON postings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION assert_entry_balanced();
```

- `AFTER INSERT ON postings`: fire after each posting row is inserted.
- `DEFERRABLE INITIALLY DEFERRED`: but do not check yet; wait until `COMMIT`. The two rows of an entry are inserted one after the other and are balanced only when both are there.
- The function counts the rows of the entry, sums them, and counts distinct accounts and currencies; it raises an error with code `check_violation` unless there are exactly 2 rows, the sum is 0, there are 2 accounts and 1 currency.
- A second trigger (`journal_entries_have_postings`) covers an entry with **no** rows, which would never fire the first trigger.

```sql
CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

A trigger that always raises an error: corrections are new balanced entries (a fee correction, a reversal of a duplicate), never edits.

### Keeping the results honest

```sql
CONSTRAINT reconciliation_breaks_natural_key UNIQUE (run_date, provider_txn_id, kind, cause),
CONSTRAINT reconciliation_breaks_cleared_on_matches_status CHECK ((cleared_on IS NOT NULL) <= (status = 'CLEARED'))
```

- The **natural key** `(run_date, provider_txn_id, kind, cause)` means a break is identified by what it is about; reconciling the same day again finds the same row and updates it.
- The second constraint compares two true/false values: `a <= b` on booleans means "if `a` is true then `b` is true". So a `cleared_on` date may exist only when the status is `CLEARED`.
- `reconciliation_runs.run_date` is the primary key, and `reconciliation_breaks.run_date` references it, so a break cannot exist for a day that has no run. The code saves the run first for this reason.

### Locks used by the program

```sql
SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))
```

`pg_advisory_xact_lock(bigint)` takes a transaction-level advisory lock: it waits if another transaction holds the same number, and releases automatically at `COMMIT` or `ROLLBACK` (documented behavior). `hashtextextended(text, seed)` is a built-in hash function that turns the provider transaction id into a 64-bit number (an internal helper that the manual does not list). Two resolvers working on one provider transaction therefore take turns, so each sees the other's committed work. `SELECT ... FROM reconciliation_breaks WHERE id = %s FOR UPDATE` serializes work on one break row, which is why resolving the same break twice in parallel posts once.

### The session runs in UTC

```python
make_conninfo(..., options="-c timezone=UTC")
```

Each connection starts with `timezone=UTC`, so "the business day" always means a UTC calendar day, whatever the server's or the machine's zone.

### Why the schema file starts with `BEGIN` and ends with `COMMIT`

If any statement fails, the transaction aborts and the database is not left half-built (`psql -v ON_ERROR_STOP=1` makes the script stop at the error).

## Try it yourself

1. Start the database and load the schema and seed data:

   ```bash
   make up
   ```

   Expected: a container named `payments-reconciliation-postgres-1` becomes healthy; the scripts print no errors.

2. Open a shell into it:

   ```bash
   docker compose exec postgres psql -U reconciliation -d reconciliation
   ```

   Then `\dt` (seven tables) and `select sum(amount_minor) from postings;` (expected `0`).

3. Try to break the ledger (inside `psql`):

   ```sql
   update postings set amount_minor = 1 where id = 1;
   ```

   Expected: `ERROR:  postings is append-only; post a correcting entry instead`.

4. Try to make a duplicate break row (inside `psql`, after `make reconcile-sample`):

   ```sql
   insert into reconciliation_breaks (run_date, provider_txn_id, kind, cause, status, currency, detail, note, created_at, updated_at)
   select run_date, provider_txn_id, kind, cause, status, currency, detail, note, created_at, updated_at from reconciliation_breaks limit 1;
   ```

   Expected: `ERROR:  duplicate key value violates unique constraint "reconciliation_breaks_natural_key"`.

5. Run the lesson queries: `make queries`. Expected: the sections `== 1. Trial balance ...` to `== 10. What the resolutions posted, by operation`, with the ledger sum `0`.

6. Stop and delete everything: `make down`.

## Mistakes people make

- **Using `FLOAT` for money.** Binary floating point cannot hold 0.10 exactly. Use integers in minor units.
- **Checking invariants only in the application.** A second program, a script or a typo can bypass it; the database cannot be bypassed.
- **Copying `fsync=off` to production.** See above; it is for disposable data only.
- **Making a column unique because it "should" be.** Here the ledger under test may hold duplicates; a unique constraint would hide the very break the job exists to find.
- **Assuming a deferred constraint failed at the statement.** It fails at `COMMIT`; the tests call `SET CONSTRAINTS ALL IMMEDIATE` to see the error early.
- **Forgetting the time zone.** A "day" computed in the server's local zone shifts; the connection option fixes UTC.

## Check yourself

1. What does `COMMIT` do? *Makes all changes of the transaction permanent and visible to others.*
2. Why is `postings_entry_balanced` deferred? *The two rows of an entry are inserted one at a time; the balance can only be checked when both are in.*
3. Why is `payments.provider_txn_id` not unique? *Duplicates in the ledger are a break the job must be able to see.*
4. When is an advisory transaction lock released? *At the end of the transaction, by commit or rollback.*
5. What makes a second run of the same day update instead of add break rows? *The unique natural key `(run_date, provider_txn_id, kind, cause)`.*

## Further reading

- PostgreSQL 18 documentation: https://www.postgresql.org/docs/18/
- Transactions: https://www.postgresql.org/docs/18/tutorial-transactions.html
- Constraints: https://www.postgresql.org/docs/18/ddl-constraints.html
- CREATE TRIGGER (constraint triggers): https://www.postgresql.org/docs/18/sql-createtrigger.html
- Advisory locks: https://www.postgresql.org/docs/18/explicit-locking.html#ADVISORY-LOCKS
- WAL settings (`fsync`, `synchronous_commit`): https://www.postgresql.org/docs/18/runtime-config-wal.html
- The official `postgres` Docker image: https://hub.docker.com/_/postgres
