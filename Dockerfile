# The HTTP API as an image (mount the provider's settlement files at /data/settlement): non-root user, hash-pinned dependencies, the health endpoint as its check.
#   docker build -t payments-reconciliation:dev .
#   docker compose --profile app up --build        (PostgreSQL and the API together)
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    API_HOST=0.0.0.0 \
    API_PORT=8462 \
    SETTLEMENT_DIR=/data/settlement

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data/settlement && chown app /data/settlement
USER app
VOLUME /data/settlement

EXPOSE 8462
HEALTHCHECK --interval=15s --timeout=6s --retries=3 CMD ["payments-reconciliation-health"]
CMD ["payments-reconciliation-serve"]
