"""Configuration from the environment, validated once at startup (twelve-factor). Secrets only from here."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from payments_reconciliation.domain.money import CURRENCY_EXPONENTS

_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class ConfigurationError(ValueError):
    """An environment variable is missing or has an unusable value."""


def _text(environ: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = environ.get(name, default if default is not None else "").strip()
    if not value:
        raise ConfigurationError(f"{name} must be set and not empty")
    return value


def _positive_int(environ: Mapping[str, str], name: str, default: int, *, maximum: int | None = None) -> int:
    raw = environ.get(name, str(default)).strip()
    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} must be a positive integer, got {raw!r}")
    if maximum is not None and int(raw) > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}, got {raw}")
    return int(raw)


def _clock_time(environ: Mapping[str, str], name: str, default: str) -> time:
    raw = environ.get(name, default).strip()
    try:
        hour, minute = raw.split(":")
        return time(int(hour), int(minute))
    except ValueError as error:
        raise ConfigurationError(f"{name} must be HH:MM in UTC, got {raw!r}") from error


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """The PostgreSQL part of the configuration; the password is never printed."""

    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)  # never printed or logged
    connect_timeout_seconds: int
    pool_max_size: int


@dataclass(frozen=True, slots=True)
class ApiSettings:
    """Where the HTTP API listens and how long it waits for in-flight requests when told to stop."""

    host: str
    port: int
    shutdown_grace_seconds: int


@dataclass(frozen=True, slots=True)
class ReconciliationSettings:
    """The rules of the daily job."""

    currency: str
    settlement_dir: Path
    cutoff: time
    max_carry_days: int
    provider_fee_basis_points: int


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the programs read from the environment. See .env.example for each variable."""

    postgres: PostgresSettings
    api: ApiSettings
    reconciliation: ReconciliationSettings
    log_level: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Read and validate; raises `ConfigurationError` naming the first bad variable."""
        env = os.environ if environ is None else environ
        level = _text(env, "LOG_LEVEL", "INFO").upper()
        if level not in _LEVELS:
            raise ConfigurationError(f"LOG_LEVEL must be one of {sorted(_LEVELS)}, got {level!r}")
        currency = _text(env, "CURRENCY", "USD").upper()
        if currency not in CURRENCY_EXPONENTS:
            raise ConfigurationError(f"CURRENCY must be one of {sorted(CURRENCY_EXPONENTS)}, got {currency!r}")
        return cls(
            postgres=PostgresSettings(
                host=_text(env, "POSTGRES_HOST", "localhost"),
                port=_positive_int(env, "POSTGRES_PORT", 54612, maximum=65535),
                database=_text(env, "POSTGRES_DB", "reconciliation"),
                user=_text(env, "POSTGRES_USER", "reconciliation"),
                password=_text(env, "POSTGRES_PASSWORD"),
                connect_timeout_seconds=_positive_int(env, "POSTGRES_CONNECT_TIMEOUT_SECONDS", 5),
                pool_max_size=_positive_int(env, "DB_POOL_MAX_SIZE", 10),
            ),
            api=ApiSettings(
                host=_text(env, "API_HOST", "127.0.0.1"),
                port=_positive_int(env, "API_PORT", 8462, maximum=65535),
                shutdown_grace_seconds=_positive_int(env, "SHUTDOWN_GRACE_SECONDS", 10),
            ),
            reconciliation=ReconciliationSettings(
                currency=currency,
                settlement_dir=Path(_text(env, "SETTLEMENT_DIR", "var/settlement")),
                cutoff=_clock_time(env, "SETTLEMENT_CUTOFF_UTC", "23:00"),
                max_carry_days=_positive_int(env, "MAX_CARRY_DAYS", 2),
                provider_fee_basis_points=_positive_int(env, "PROVIDER_FEE_BPS", 290, maximum=10_000),
            ),
            log_level=level,
        )
