# Dockerfile

## What it is

A `Dockerfile` is a text file of instructions that tells `docker build` how to assemble an **image**: start from a base image, copy files in, run commands, and say what the container should do when it starts. It is a file format with its own keywords, written in capitals (`FROM`, `COPY`, `RUN`). Each instruction that changes files makes one cached **layer**.

## Why this repository uses it

To package the HTTP API so it can run anywhere Docker runs, the way a deployment platform would run it: installed from the hash-pinned lock, as a non-root user, with a health check and a clean `SIGTERM` shutdown. The lesson does not require deployment, but a service you cannot package is hard to trust.

## Where it appears

| Path | Role |
| --- | --- |
| `Dockerfile` | The recipe (29 lines). |
| `.dockerignore` | What `COPY` may not see. |
| `Makefile` target `docker-build` | `docker build -t payments-reconciliation:dev .` |
| `docker-compose.yml`, service `api` | `build: .` builds from this file; used with `--profile app`. |

## How it works underneath

`docker build .` sends the **build context** (the folder, minus `.dockerignore` entries) to Docker. Docker runs the instructions top to bottom. For each, it checks the cache: if the instruction and the files it uses are unchanged since the last build, it reuses the stored layer; otherwise it runs it and every instruction after it again. So put things that change rarely first and things that change often last.

## Walkthrough of this repository's use

```dockerfile
FROM python:3.12.8-slim-bookworm@sha256:2199a628...
```

`FROM` picks the base image. `python:3.12.8-slim-bookworm` is the official Python 3.12.8 image on Debian 12 ("bookworm") in its slim variant (fewer packages, smaller). `@sha256:...` pins the exact content by digest, so even if the tag is re-published, this build is unchanged.

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8462 \
    SETTLEMENT_DIR=/data/settlement
```

`ENV` sets environment variables for the build and for the running container. `\` continues the line.

- `PYTHONDONTWRITEBYTECODE=1`: do not write `.pyc` files (smaller image).
- `PYTHONUNBUFFERED=1`: send output straight to the log instead of holding it in a buffer, so log lines appear at once.
- `PIP_NO_CACHE_DIR=1`: pip does not keep a download cache in the image.
- `PIP_DISABLE_PIP_VERSION_CHECK=1`: pip does not call out to check for its own update.
- `API_HOST=0.0.0.0`: listen on all network interfaces. Inside a container, `127.0.0.1` would be reachable only from inside it.
- `API_PORT=8462`: the port the API listens on.
- `SETTLEMENT_DIR=/data/settlement`: the folder where the program looks for the provider's daily CSV files (read by `Settings`, see `environment-variables-and-dotenv.md`).

```dockerfile
WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
```

- `WORKDIR` sets (and creates) the working folder for the following instructions.
- `COPY requirements.lock ./` copies one file from the build context into `/app`.
- `RUN` executes a command at build time and keeps the result. `--require-hashes` makes pip accept only the files whose hashes are in the lock.

The lock is copied and installed **before** the source. The install is the slow step; because the lock changes rarely, Docker reuses this layer when only code changed.

```dockerfile
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .
```

Now the project itself: `pyproject.toml` (and `README.md`, which it references as the long description) and the `src` folder. `pip install --no-deps .` installs this package and its command-line programs; `--no-deps` because the locked dependencies are already in.

```dockerfile
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data/settlement && chown app /data/settlement
USER app
VOLUME /data/settlement
```

- `useradd ...` creates a system user named `app` with a fixed numeric id and no home folder. A fixed numeric uid lets platforms that require "a non-root numeric user" verify it.
- `&& mkdir -p /data/settlement && chown app /data/settlement` (on the same `RUN`, so one layer): while still root, create the settlement folder and give it to `app`. `&&` runs the next command only if the previous one succeeded.
- `USER app` switches to that user. Everything after, including the running service, is non-root.
- `VOLUME /data/settlement` declares that this folder holds data that comes from outside the image. The provider's files are not part of the image; Compose mounts a host folder there (read-only, see `docker-compose.md`).

```dockerfile
EXPOSE 8462
HEALTHCHECK --interval=15s --timeout=6s --retries=3 CMD ["payments-reconciliation-health"]
CMD ["payments-reconciliation-serve"]
```

- `EXPOSE` documents the port; it does not publish it (publishing is `-p` or Compose `ports`).
- `HEALTHCHECK`: every 15 seconds run `payments-reconciliation-health` (a program installed from `pyproject.toml`; it calls `/health`); fail it if it takes more than 6 seconds; mark the container `unhealthy` after 3 failures in a row.
- `CMD`: the default command, `payments-reconciliation-serve`. The square-bracket form is the **exec form**: Docker runs the program directly, not through a shell, so it is process 1 and receives `SIGTERM` itself, which is what lets the server shut down gracefully.

<!-- listing:Dockerfile -->
### `Dockerfile` (29 lines)

```dockerfile
# The HTTP API as an image (mount the provider's settlement files at /data/settlement): non-root user, hash-pinned dependencies, the health endpoint as its check.
#   docker build -t payments-reconciliation:dev .
#   docker compose --profile app up --build        (PostgreSQL and the API together)
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
```

The first comment line says the image runs the HTTP API and that the provider's settlement files are meant to be mounted at `/data/settlement` (a volume), with a non-root user, hash-pinned dependencies and the health endpoint as its check; the other comment lines show how to build and run it. `FROM python:3.12.8-slim-bookworm@sha256:...` picks the starting image: official Python 3.12.8 on Debian 12 in its slim variant, pinned by its content digest so the bytes cannot change.

```dockerfile
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8462 \
    SETTLEMENT_DIR=/data/settlement
```

`ENV` sets environment variables for the build and for the running container (`\` continues the line). `PYTHONDONTWRITEBYTECODE=1` stops Python writing `.pyc` files; `PYTHONUNBUFFERED=1` sends output at once so logs appear immediately; `PIP_NO_CACHE_DIR=1` keeps pip's download cache out of the image; `PIP_DISABLE_PIP_VERSION_CHECK=1` stops pip checking for its own update; `API_HOST=0.0.0.0` makes the API listen on all interfaces (inside a container `127.0.0.1` would be unreachable from outside); `API_PORT=8462` is the port; `SETTLEMENT_DIR=/data/settlement` tells the program where to find the provider's files.

```dockerfile
WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
```

`WORKDIR /app` creates and enters the folder `/app` for the following instructions. `COPY requirements.lock ./` copies the lock from the build folder into it. `RUN pip install --require-hashes -r requirements.lock` installs exactly the locked runtime packages. This comes **before** the source is copied so Docker can reuse this slow layer when only code changes.

```dockerfile
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .
```

Now the project: `pyproject.toml` and `README.md` (which the package metadata refers to) and the `src` folder are copied, and `RUN pip install --no-deps .` installs this package and its commands without resolving anything.

```dockerfile
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data/settlement && chown app /data/settlement
USER app
VOLUME /data/settlement
```

`useradd --system --uid 10001 --no-create-home app` creates an unprivileged system user with a fixed number and no home folder. `&& mkdir -p /data/settlement && chown app /data/settlement` (the `&&` runs the next command only if the previous one succeeded) creates the settlement folder and gives it to that user, so the program can read it (and write the files of a simulated day). `USER app` makes everything after, including the running service, use that user. `VOLUME /data/settlement` declares the folder as a mount point: a place where Docker expects storage from outside to be attached.

```dockerfile
EXPOSE 8462
HEALTHCHECK --interval=15s --timeout=6s --retries=3 CMD ["payments-reconciliation-health"]
CMD ["payments-reconciliation-serve"]
```

`EXPOSE` documents the port (it does not publish it). `HEALTHCHECK --interval=15s --timeout=6s --retries=3 CMD ["payments-reconciliation-health"]` makes Docker run the health command every 15 seconds, give it 6 seconds, and mark the container unhealthy after 3 failures in a row. `CMD ["payments-reconciliation-serve"]` is the command the container runs; the square-bracket form runs the program directly (not through a shell), so it is process 1 and receives `SIGTERM` itself.

<!-- /listing:Dockerfile -->

## Try it yourself

1. Build it (needs Docker and network, can take minutes):

   ```bash
   make docker-build
   ```

   Expected: the last lines mention `naming to docker.io/library/payments-reconciliation:dev`.

2. Check the user and the configured check:

   ```bash
   docker run --rm --entrypoint id payments-reconciliation:dev
   docker inspect --format '{{json .Config.Healthcheck}}' payments-reconciliation:dev
   ```

   Expected: `uid=10001(app) gid=...` and a JSON object with `"Test":["CMD","payments-reconciliation-health"]`.

3. See layers: `docker history payments-reconciliation:dev`. Expected: one line per instruction with sizes; the `pip install` of the lock is the largest.

4. Run the API with its database: `docker compose --profile app up --build`, then in another terminal `make health`.

## Mistakes people make

- **Copying everything first, then installing.** Any code edit then invalidates the install layer. Copy the lock, install, then copy the source.
- **Shell-form `CMD` (`CMD payments-reconciliation-serve`).** The shell becomes process 1 and may not pass `SIGTERM` on.
- **Binding to `127.0.0.1`** inside the container.
- **Running as root.**
- **Unpinned base image.** The bytes change under you.

## Check yourself

1. Why is the lock copied before `src`? *To reuse the cached dependency layer when only code changes.*
2. What does `USER app` change? *Later instructions and the running service no longer run as root.*
3. What does `EXPOSE` do? *Documents the port; it does not publish it.*
4. Why the exec form of `CMD`? *So the program is process 1 and gets signals directly.*
5. What does the digest after `@` guarantee? *The exact same base image content on every build.*

## Further reading

- Dockerfile reference: https://docs.docker.com/reference/dockerfile/
- Best practices for Dockerfiles: https://docs.docker.com/build/building/best-practices/
- Official Python image: https://hub.docker.com/_/python
