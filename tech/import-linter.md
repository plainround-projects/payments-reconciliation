# import-linter

## What it is

import-linter is a command-line tool (`lint-imports`) that checks **which modules are allowed to import which**. You write the rules once, in a config file, as "contracts". It reads all the `import` statements in the package, builds a graph of who imports whom, and fails if any contract is broken.

## Why this repository uses it

The design is **layered**: the domain (money rules) must know nothing about the web, the database or the framework, so it can be tested alone and never changes because a library does. A sentence in a README does not stop someone adding `import fastapi` to a domain file next month. A failing check does.

## Where it appears

| Path | Role |
| --- | --- |
| `pyproject.toml`, `[tool.importlinter]` | The two contracts. |
| `Makefile` target `lint` | Runs `lint-imports`. |
| `.import_linter_cache/` | Cache (ignored by git). |
| `src/payments_reconciliation/{domain,application,infrastructure,interfaces}` | The layers being checked. |

## How it works underneath

The tool uses a library called Grimp to parse every module under `root_package` and record each import as an edge (`A imports B`). A contract is a rule about edges. The `layers` kind orders packages from highest to lowest and forbids an edge that points from a lower layer up to a higher one. The `forbidden` kind forbids any path from the source modules to the listed modules, including **indirect** paths (A imports B imports fastapi).

## Walkthrough of this repository's use

```toml
[tool.importlinter]
root_package = "payments_reconciliation"
include_external_packages = true
```

- `root_package`: the package to analyze.
- `include_external_packages = true`: also track imports of third-party packages (`fastapi`, `psycopg`), which the second contract needs.

```toml
[[tool.importlinter.contracts]]
name = "Layers: interfaces -> infrastructure -> application -> domain, never upward"
type = "layers"
layers = [
    "payments_reconciliation.interfaces",
    "payments_reconciliation.infrastructure",
    "payments_reconciliation.application",
    "payments_reconciliation.domain",
]
```

The list is ordered from the top layer to the bottom. Each layer may import the ones below it, never the ones above. `interfaces` (HTTP, programs) may use everything; `domain` may import nothing from the other three. The adapters in `infrastructure` implement ports defined in `application`, so the arrow of knowledge points inward.

```toml
[[tool.importlinter.contracts]]
name = "Domain and application know nothing about PostgreSQL, HTTP or the web framework"
type = "forbidden"
source_modules = ["payments_reconciliation.domain", "payments_reconciliation.application"]
forbidden_modules = ["psycopg", "psycopg_pool", "fastapi", "starlette", "uvicorn", "httpx", "pydantic"]
```

Neither the domain nor the use cases may import (even through another module) any of these libraries. That is why the use cases talk to the database through `UnitOfWork` and friends, which are `Protocol` classes in `application/ports.py`, and why only `infrastructure/postgres.py` imports `psycopg`.

### Reading a failure

If someone adds `import fastapi` to `domain/money.py`, the output names the importer and line:

```
payments_reconciliation.domain is not allowed to import fastapi:

-   payments_reconciliation.domain.money -> fastapi (l.109)
```

and the application layer is reported too, because it imports `money`: `application.ports -> domain.money -> fastapi`. The exit status is non-zero, so `make lint` and CI fail.

## Try it yourself

1. Run it:

   ```bash
   .venv/bin/lint-imports
   ```

   Expected: `Analyzed 65 files, 240 dependencies.`, both contracts marked `KEPT`, and `Contracts: 2 kept, 0 broken.`

2. Break a rule, then restore the file:

   ```bash
   cp src/payments_reconciliation/domain/money.py /tmp/money.bak
   echo "import fastapi" >> src/payments_reconciliation/domain/money.py
   .venv/bin/lint-imports | tail -12
   cp /tmp/money.bak src/payments_reconciliation/domain/money.py
   ```

   Expected: `BROKEN` for the forbidden contract and the lines shown above.

3. Check the tool's version: `.venv/bin/lint-imports --version` (this machine: `import-linter 2.15`).

## Mistakes people make

- **Forgetting indirect imports.** The `forbidden` contract follows chains, so hiding `import fastapi` in a helper module does not help.
- **Leaving `include_external_packages` off.** Then rules about third-party libraries silently check nothing.
- **Reversing the layer order.** The first name in `layers` is the highest layer.
- **Fixing a violation by deleting the contract.** Move the code to the right layer instead.

## Check yourself

1. What does the `layers` contract forbid? *A lower layer importing a higher layer.*
2. Why is `pydantic` forbidden in `domain` and `application`? *Validation of HTTP bodies belongs at the edge; the rules of money must not depend on a web library.*
3. Does the `forbidden` contract catch indirect imports? *Yes, any import path from the source modules to the forbidden ones.*
4. Which file is the only one allowed to import `psycopg`? *`infrastructure/postgres.py` (and the tests).*
5. What happens in CI when a contract is broken? *`lint-imports` exits non-zero and `make check` fails.*

## Further reading

- import-linter (2.x) documentation: https://import-linter.readthedocs.io/en/stable/
- Contract types: https://import-linter.readthedocs.io/en/stable/contract_types.html
- Usage and configuration: https://import-linter.readthedocs.io/en/stable/usage.html
