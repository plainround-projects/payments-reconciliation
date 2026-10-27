# HTTP

## What it is

HTTP (HyperText Transfer Protocol) is the set of rules clients and servers use to talk over a network. A client sends a **request**; the server sends back a **response**. Each message is plain text with a start line, **headers** (name: value pairs) and an optional **body**. An API built on HTTP is usually called a **REST-style API** when it uses URLs for things (`/v1/days/2026-10-20/report`) and methods for actions.

## Why this repository uses it

The reconciliation job can be run by a person at the command line, and also driven by a program: a scheduler can `POST` "reconcile this day", an operator can `GET` the day's report or the worklist, and a platform can `GET /health`. The status codes tell the caller what happened: 404 means the provider's file for that day has not arrived, 422 means it arrived but is malformed.

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../interfaces/http/app.py` | The routes, headers and responses. |
| `src/.../interfaces/health_cli.py` | The client side of `/health`. |
| `tests/e2e/test_lesson_scenario.py` | Calls the running server like a client. |
| `README.md` | The endpoint table with examples. |

## How it works underneath

A request on the wire:

```
POST /v1/days/2026-10-20/reconcile HTTP/1.1
Host: 127.0.0.1:8462
X-Request-Id: 7c1f

```

- **Method**: what to do. `GET` reads and must not change anything. `POST` triggers an action.
- **Path**: which thing. `{business_date}` in the code is a variable part.
- **Status code** in the response: 2xx success, 4xx the client's mistake, 5xx the server's problem.
- A **TCP connection** carries the messages; HTTP/1.1 can reuse one connection for many requests (keep-alive).

## Walkthrough of this repository's use

### The endpoints

| Method and path | Meaning |
| --- | --- |
| `POST /v1/days/{date}/reconcile` | Read the day's settlement file, match it against the ledger, store and return the run and its breaks. Safe to call again: it updates the same break rows. |
| `POST /v1/days/{date}/resolve` | Apply the safe fixes for the day's breaks (each posts once). Safe to call again: the second pass reports `already resolved`. |
| `GET /v1/days/{date}/report` | The day's report as JSON: the first-pass run numbers, the balance-proof buckets and whether they add up (`balances`), the lists `auto_resolved`, `resolved`, `carried`, `settled_later`, `cleared_carried`, `worklist` and `open`, the ledger difference and ledger sum, and a ready-to-print `text` version. |
| `GET /v1/worklist` | Every break that needs a person, oldest first. |
| `GET /health/live` | Is the process up. |
| `GET /health` | Is the process and its database healthy (200 or 503). |

### The status codes it uses (from `app.py`)

| Status | When |
| --- | --- |
| 200 | Success. |
| 400 | A date that is not `YYYY-MM-DD`, an invalid amount, or a request that fails validation. |
| 404 | No settlement file for that day (`settlement_missing`), or no report for a day that never ran (`report_not_available`). |
| 422 | The settlement file exists but is malformed (`settlement_format`): wrong header, wrong column count, a bad amount or time. |
| 503 | `/health` only: the database is unreachable. |
| 500 | A bug; the body says only "an unexpected error occurred". |

Every error uses one shape: `{"error": {"code": "...", "message": "..."}}`.

### Headers

`X-Request-Id`: a trace id. The server keeps the one you send or invents one, and returns it, so a log line can be matched to a request. There is no authentication in this teaching build; it is listed among the simplifications.

### Why `reconcile` and `resolve` are safe to repeat

Both are `POST`, which HTTP does not promise to be repeatable, but the program makes them so: breaks are keyed by day, transaction, kind and cause, a resolved break stays resolved, and every ledger correction is posted once (a database `UNIQUE` constraint). A scheduler that retries after a timeout cannot double-post a fee correction.

## Try it yourself

With the stack up (`make up`), the scenario loaded (`make run`) and the server running (`make serve`):

1. Read the worklist:

   ```bash
   curl -s localhost:8462/v1/worklist
   ```

   Expected: JSON with one item, `ptx_0400`, cause `amount_mismatch`.

2. Read the report: `curl -s localhost:8462/v1/days/2026-10-20/report`. Expected: JSON including `"balances": true` and `"clean_transactions": 494`.

3. A day with no file:

   ```bash
   curl -i -X POST localhost:8462/v1/days/2020-01-01/reconcile
   ```

   Expected: `HTTP/1.1 404 Not Found`, `x-request-id: ...`, body with `"code":"settlement_missing"`.

4. A bad date: `curl -i -X POST localhost:8462/v1/days/tomorrow/reconcile`. Expected: `400` and `"code":"invalid_request"`.

## Mistakes people make

- **Treating `POST` as safe to retry** without a design that makes it so.
- **Using 200 for every error.** Clients cannot tell success from failure; distinct codes drive decisions.
- **Giving two meanings to one status.** Here 422 only ever means a malformed provider file.
- **Putting secrets in URLs.** URLs are logged; this API has no secrets in paths.
- **Forgetting the time zone of "a day".** The path date is a UTC calendar day.

## Check yourself

1. What does a 404 from `POST /v1/days/{date}/reconcile` mean? *The provider's settlement file for that day is missing.*
2. What does a 422 mean here? *The file is present but malformed.*
3. Why may `GET` not change data? *By definition it is safe; caches and retries assume so.*
4. Why can `reconcile` be repeated? *Breaks are keyed by their identity and updated, not added.*
5. Which header ties a response to a log line? *`X-Request-Id`.*

## Further reading

- HTTP Semantics (RFC 9110): https://www.rfc-editor.org/rfc/rfc9110
- HTTP/1.1 message syntax (RFC 9112): https://www.rfc-editor.org/rfc/rfc9112
- MDN, HTTP overview: https://developer.mozilla.org/en-US/docs/Web/HTTP
