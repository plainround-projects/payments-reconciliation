# Docker Compose

## What it is

Docker Compose describes **several containers and how they connect** in one YAML file (`docker-compose.yml`) and starts them with one command (`docker compose up`). Each container is a **service**. Compose also creates a private network so services can reach each other by name. This repository uses Compose v2, the `docker compose` command that ships with Docker Desktop.

## Why this repository uses it

`make up` needs a PostgreSQL 18 database with a known user, password, port and health check. A compose file records those settings in one reviewable place instead of a long `docker run` command in a README. The same file can also run the API next to the database (the `app` profile).

## Where it appears

| Path | Role |
| --- | --- |
| `docker-compose.yml` | Two services: `postgres` (always) and `api` (profile `app`). |
| `.env` (created from `.env.example` by `make up`) | Supplies `POSTGRES_PASSWORD` and optional overrides. Not committed. |
| `Makefile` | `up`, `down` and every `psql` call use `docker compose`. |

## How it works underneath

`docker compose up` reads the file, substitutes `${VARIABLES}` from the shell environment and `.env`, creates a network named after the project, creates each service's container, starts them in dependency order and (with `--wait`) waits for health checks. `exec` runs a command in an already-running service container. `down` stops and removes containers and the network; `-v` also removes volumes.

## Walkthrough of this repository's use

```yaml
name: payments-reconciliation

services:
```

`name` fixes the Compose **project name** (otherwise it comes from the folder name). Container names become `payments-reconciliation-postgres-1`. `services:` is the map of containers.

```yaml
postgres:
  image: postgres:18
  command: ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
```

- `image`: pull and run this image; no build.
- `command`: replaces the image's default command with `postgres` plus three `-c name=value` server settings. Each setting turns off a disk-safety step so tests run fast on a busy machine. A comment in the file says these are for a disposable database only.

```yaml
  ports:
    - "${POSTGRES_PORT:-54612}:5432"
```

`host:container`. `${POSTGRES_PORT:-54612}` means "the value of `POSTGRES_PORT`, or `54612` if unset or empty". The default is unusual on purpose, so it does not clash with a local PostgreSQL on 5432 or with other projects.

```yaml
  environment:
    POSTGRES_USER: ${POSTGRES_USER:-reconciliation}
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?copy .env.example to .env first}
    POSTGRES_DB: ${POSTGRES_DB:-reconciliation}
```

The official image reads these variables the first time it starts, creating the role and database. `${NAME:?message}` aborts `docker compose` with that message if `NAME` is unset or empty, so no password is ever written in the file or defaulted to something guessable.

```yaml
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
    interval: 5s
    timeout: 20s
    retries: 30
```

Every 5 seconds run the test (through a shell, so the variables expand inside the container; `$$` stops Compose expanding them itself). Each try may take 20 seconds. After 30 failed tries the service is `unhealthy`. `docker compose up --wait` waits for `healthy`.

```yaml
api:
  profiles: ["app"]
  build: .
  image: payments-reconciliation:dev
  depends_on:
    postgres:
      condition: service_healthy
  ports:
    - "${API_PORT:-8462}:8462"
  environment:
    POSTGRES_HOST: postgres
    POSTGRES_PORT: "5432"
    ...
    API_HOST: 0.0.0.0
    API_PORT: "8462"
    SETTLEMENT_DIR: /data/settlement
  volumes:
    - ./var/settlement:/data/settlement:ro
```

- `profiles: ["app"]`: this service starts only when you ask (`--profile app`). Plain `make up` starts just the database.
- `build: .` builds the image from the `Dockerfile` in this folder; `image` names the result.
- `depends_on` with `condition: service_healthy`: start the API only after the database is healthy.
- Inside the Compose network the API reaches the database by the **service name** `postgres` on its container port `5432` (not on the host port 54612).
- The `environment` list passes the settings the Python program reads (see `environment-variables-and-dotenv.md`), including `SETTLEMENT_DIR`, the folder inside the container where it looks for the provider's files.
- `volumes: - ./var/settlement:/data/settlement:ro` is a **bind mount**: the host folder `var/settlement` (where `make run` and the simulator write the CSV files) appears inside the container at `/data/settlement`. `:ro` makes it read-only for the container, which only needs to read the provider's files. Like the Dockerfile's `VOLUME`, it keeps the daily files outside the image.

<!-- listing:docker-compose.yml -->
### `docker-compose.yml` (42 lines)

```yaml
# PostgreSQL 18 for local use only; the API can run as a container too (profile "app").
name: payments-reconciliation

services:
```

The comment says this is PostgreSQL 18 for local use, with the API as an optional container under the profile `app`. `name: payments-reconciliation` fixes the Compose project name, which prefixes container, network and volume names. `services:` is the map of containers.

```yaml
  postgres:
    image: postgres:18
    # A disposable local database: durability is switched off so the tests and demos do not depend on disk speed, and a
    # crash recovers instantly. Never use these settings for data you want to keep.
    command: ["postgres", "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
    ports:
      - "${POSTGRES_PORT:-54612}:5432"
```

The database service: `image: postgres:18` is used as published. The comment above `command` explains the three server flags `fsync=off`, `synchronous_commit=off` and `full_page_writes=off`: they trade crash-safety for speed, acceptable only for a disposable database. `ports: - "${POSTGRES_PORT:-54612}:5432"` publishes the container's port 5432 on the computer's port 54612 (the text `${NAME:-default}` means "the variable NAME, or default if unset").

```yaml
    environment:
      # Credentials come from .env (make up copies .env.example the first time); none is written here.
      POSTGRES_USER: ${POSTGRES_USER:-reconciliation}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?copy .env.example to .env first}
      POSTGRES_DB: ${POSTGRES_DB:-reconciliation}
```

`environment` gives the image its first-start settings: `POSTGRES_USER` and `POSTGRES_DB` (default `reconciliation`) and `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?copy .env.example to .env first}`: the `:?message` form makes Compose stop with that message if the variable is unset, so no password is written in the file.

```yaml
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 5s
      timeout: 20s
      retries: 30
```

The health check: `test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]` runs `pg_isready` through a shell inside the container (`$$` stops Compose from filling the variables itself; `-h 127.0.0.1` forces a network check, because during first start the image runs a temporary server that listens only on a local socket). It runs every 5 seconds, may take 20, and after 30 failures the service is unhealthy; `up --wait` waits for healthy. The longer limits leave room for a busy machine.

```yaml
  api:
    profiles: ["app"]
    build: .
    image: payments-reconciliation:dev
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "${API_PORT:-8462}:8462"
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: "5432"
      POSTGRES_DB: ${POSTGRES_DB:-reconciliation}
      POSTGRES_USER: ${POSTGRES_USER:-reconciliation}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?copy .env.example to .env first}
      API_HOST: 0.0.0.0
      API_PORT: "8462"
      SETTLEMENT_DIR: /data/settlement
    volumes:
      - ./var/settlement:/data/settlement:ro
```

The optional API service. `profiles: ["app"]` means it starts only with `--profile app`. `build: .` builds the image from the `Dockerfile` here and `image:` names it. `depends_on: postgres: condition: service_healthy` starts the API only after the database is healthy. `ports` publishes 8462. `environment` passes the settings the program reads; `POSTGRES_HOST: postgres` uses the service name as the host, which Compose's private network resolves to the database container, and the container-side port is 5432. `SETTLEMENT_DIR: /data/settlement` is where the program looks for the provider's files, and `volumes: - ./var/settlement:/data/settlement:ro` mounts the host folder `var/settlement` there; `:ro` means read-only for the container.

<!-- /listing:docker-compose.yml -->

## Try it yourself

1. Validate and print the final file with variables filled in:

   ```bash
   docker compose config
   ```

   Expected: the merged YAML; with no `.env` you get the message `copy .env.example to .env first`.

2. Start only the database: `make up`, then `docker compose ps` (the `api` service does not appear).

3. Start both: `docker compose --profile app up --build -d`, then `docker compose ps` (two services) and `make health`.

4. Stop everything: `make down`.

## Mistakes people make

- **Putting passwords in the file.** Use `.env` (ignored by git) and the `:?` guard.
- **Single `$` in a health check command.** Compose would expand it from the host environment. Write `$$`.
- **Using the host port from another container.** Inside the network use the service name and the container port.
- **Assuming `depends_on` waits for readiness.** Only with `condition: service_healthy`.
- **Forgetting the profile.** The `api` service silently does not start without `--profile app`.

## Check yourself

1. What does `${POSTGRES_PORT:-54612}` do? *Uses `POSTGRES_PORT`, or 54612 if it is unset.*
2. What does `${POSTGRES_PASSWORD:?message}` do when the variable is missing? *Stops Compose with the message.*
3. What is a profile for? *To start a service only on request.*
4. Why does the API use `POSTGRES_HOST: postgres`? *Compose's network resolves the service name to the database container.*
5. What does `--wait` wait for? *The services' health checks to pass.*

## Further reading

- Compose file reference (Compose Specification): https://docs.docker.com/reference/compose-file/
- Services and `healthcheck`: https://docs.docker.com/reference/compose-file/services/
- Interpolation (`:-`, `:?`): https://docs.docker.com/reference/compose-file/interpolation/
- Profiles: https://docs.docker.com/compose/how-tos/profiles/
- `docker compose up --wait`: https://docs.docker.com/reference/cli/docker/compose/up/
