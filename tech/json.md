# JSON

## What it is

JSON (JavaScript Object Notation) is a text format for structured data. It has objects (`{"key": value}`), arrays (`[1, 2]`), strings, numbers, `true`/`false` and `null`. It is the common language of web APIs: the responses of the HTTP API in this repository are JSON.

## Why this repository uses it

The API returns the day's run, its breaks, the resolutions, the report and the worklist as JSON. Amounts need care: JSON numbers are usually read as floating point, which cannot hold 0.10 exactly, so money crosses the API as integer minor units and as decimal **strings**.

## Where it appears

| Path | Role |
| --- | --- |
| `src/.../interfaces/http/app.py` | `JSONResponse(...)` for every response. |
| `src/.../application/views.py` | The functions that turn runs, breaks and resolutions into dicts ready for JSON (`run_view`, `break_view`, `resolution_view`, `report_view`). |
| `tests/e2e/test_lesson_scenario.py` | `.json()` on responses, and assertions on the fields. |

## How it works underneath

On the wire JSON is just text, so the sender **serializes** a value to text and the receiver **parses** the text back. Python's `json` module does both (`dumps`, `loads`), and FastAPI's `JSONResponse` serializes a dict for you and sets `Content-Type: application/json`. A Python dict becomes a JSON object, a list a JSON array, `None` becomes `null`, and `True` becomes `true`.

## Walkthrough of this repository's use

### The report as JSON

The e2e test reads the report of the planted day:

```python
report = httpx.get(f"{server.url}/v1/days/2026-10-20/report", timeout=30).json()
assert report["balances"] is True and report["clean_transactions"] == 494
assert [line["provider_txn_id"] for line in report["worklist"]] == ["ptx_0400"]
assert [line["provider_txn_id"] for line in report["resolved"]] == ["ptx_0200", "ptx_0215", "ptx_0300"]
```

The whole report object (built by `report_view`) has these top-level keys: `business_date`, `currency`, `run`, `settled_rows`, `settled_total_minor`, `buckets_minor`, `balances`, `clean_transactions`, `auto_resolved`, `resolved`, `carried`, `settled_later`, `cleared_carried`, `worklist`, `open`, `ledger_difference_minor`, `ledger_sum_minor` and `text`. Names ending in `_minor` are integers in minor units.

- `report["balances"]` is a JSON `true`: the report's buckets add up to the settlement report's settled total (the balance proof).
- `report["worklist"]` and `report["resolved"]` are JSON arrays of objects, one per break.

### Why the "view" layer exists

Domain objects (`Money`, `Break`, `RunRecord`) are Python dataclasses; JSON has no such thing. The view functions convert each to plain dicts, lists, strings and integers, with amounts as text from `Money.format()` (`"42.50"`). Keeping the conversion in one file (`views.py`) means the HTTP layer and the command-line report read the same data.

### One error shape

```json
{"error": {"code": "settlement_missing", "message": "no settlement file for 2020-01-01 at var/settlement/settlement_2020-01-01.csv"}}
```

### What JSON cannot hold

No dates (they travel as ISO-8601 strings, `"2026-10-20"`), no exact big decimals (hence integer minor units and string amounts), no comments, no trailing commas.

### Where JSON is not used

The provider's settlement report is **CSV**, not JSON (see `csv.md`). The database stores no JSON in this repository.

## Try it yourself

1. Canonical form of a dict:

   ```bash
   .venv/bin/python -c "
   import json
   print(json.dumps({'b': 2, 'a': 1}, sort_keys=True, separators=(',', ':')))"
   ```

   Expected: `{"a":1,"b":2}`.

2. See why floats are avoided:

   ```bash
   .venv/bin/python -c "print(0.1 + 0.2)"
   ```

   Expected: `0.30000000000000004`.

3. With the server running and the scenario loaded (`make run`, `make serve`): `curl -s localhost:8462/v1/worklist | .venv/bin/python -m json.tool`. Expected: indented JSON with one break.

## Mistakes people make

- **Floats for money in JSON.** Use integers or strings.
- **Trusting a number's type.** `"4250"` and `4250` are different values.
- **Forgetting `null`.** A break found only in the settlement file has `null` for the ledger amount.
- **Returning domain objects directly.** Convert them in a view function so the API's shape is deliberate.

## Check yourself

1. How do dates travel in JSON? *As strings (ISO-8601).*
2. Why are amounts also sent as strings? *To show 42.50 without float rounding.*
3. What does a Python `None` become in JSON? *`null`.*
4. Where does the repository keep the conversion to JSON-ready dicts? *In `application/views.py`.*
5. Is the provider's report JSON? *No, it is CSV.*

## Further reading

- JSON (ECMA-404): https://ecma-international.org/publications-and-standards/standards/ecma-404/
- Python `json`: https://docs.python.org/3.12/library/json.html
- FastAPI `JSONResponse`: https://fastapi.tiangolo.com/advanced/custom-response/
